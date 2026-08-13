# ADR-020: 四端 TLS 接入与自签 CA 信任分发

## Status: Accepted (2026-08-13，项目总监裁决)

> 编号说明：team-lead 原始指派为「ADR-018」，但 `docs/decisions/` 下已有
> `ADR-018-local-privacy-data.md` 与 `ADR-019-windows-sidecar-credential-store.md`，
> 018/019 均被占用。本决策按序升格为 **ADR-020**，不覆盖既有 ADR。
>
> 项目总监补充安全红线：A2 把自签 CA 装入用户受信根库属「受信面扩张」，**必须在
> 安装/首启明确告知用户**（隐私说明 + 首次安装提示），不得静默安装；卸载时按
> thumbprint 干净移除（联动阶段 D4）。签名后换正式 CA 后即移除自签 CA。

## Background

TLS 后端底座已完成（commit 004b057）：自签 CA + 服务端证书生成于 `certs/`
（`ca.crt` 根、`server.crt` 服务端，SAN 覆盖 `localhost` / `jax-pet.local` /
`127.0.0.1` / `::1`），`backend/run.py` 按 `TLS_CERTFILE` / `TLS_KEYFILE` 启用
HTTPS/WSS，生产 fail-closed。实测信任 CA 返回 200、不信任拒绝、WSS pong 通。

但四端中三端仍走明文：sidecar `signUrl`、pet-ui `WS_URL`/`PUSH_API`、Android
`session_base_url`。阶段 A 要完成「三端从明文切加密 + 信任自签 CA」，且必须满足：

1. 信任是「真实链验证到 ca.crt」，绝不 `verify=false` / `ignore-certificate-errors`。
2. 自签 CA 是过渡（营业执照后换正式 CA），换证书 = 替换 `ca.crt`，零代码改动。
3. `ca.key`（私钥）只留在 `certs/`（已 gitignore），四端只分发 `ca.crt` 公钥。

## Decision

### 核心信任锚原则（所有端共享）

- **单一信任锚 = `certs/ca.crt` 公钥**；每端在运行时读取 `ca.crt` 内容建立信任，
  **源码中不硬编码任何指纹/证书常量**。
- 换正式 CA 的迁移语义：替换各端分发的 `ca.crt` 文件 → 各端下次启动重读 → 零代码改动。
- 四端连接目标统一为 loopback `127.0.0.1`，故 SAN `127.0.0.1`/`localhost` 已覆盖；
  `jax-pet.local` 留给未来 LAN 发现，本阶段不用。

### A1 sidecar（Electron/Node）

`signUrl` 走 HTTPS 连后端 8000；`bridgeUrl`（rtc_bridge 19092）是 loopback-only
内部进程，**不在 TLS 范围，保持 `ws://`**，若未来绑定非 loopback 必须同步升 wss。

改动点：

1. `sidecar/config.js:27`：`signUrl` 默认 `'http://127.0.0.1:8000'` →
   `'https://127.0.0.1:8000'`。`bridgeUrl` 不变。
2. `pet-ui/src-tauri/tauri.conf.json` `bundle.resources`：新增
   `"certs/ca.crt": "certs/ca.crt"`，把 CA 公钥打包进安装包。
3. `pet-ui/src-tauri/src/sidecar.rs`：`SidecarSpec` 新增 `ca_cert_path: PathBuf`；
   `spawn_with_credential()` 的 `Command` 新增 `.env("NODE_EXTRA_CA_CERTS", ca_cert_path)`。
   **注入点 = 这里，注入者 = Tauri Rust supervisor（jax-pet.exe），用 `CREATE_NO_WINDOW` 拉起前设置子进程 env。**
4. `pet-ui/src-tauri/src/main.rs` `resolve_sidecar_spec()`：由 `app.path().resource_dir()`
   解析 `ca_cert_path = resource_dir.join("certs/ca.crt")`，与 sidecar 二进制同源。
5. `sidecar/main.js`：`app.whenReady()` 前读取 `process.env.NODE_EXTRA_CA_CERTS` 指向的
   `ca.crt`，计算 SHA-256 指纹，注册 `app.on('certificate-error', ...)`：仅当证书链
   （leaf 或 issuer）指纹命中该 CA 指纹时 `callback(true)`，否则 `callback(false)`。

信任链建立：Tauri 打包 ca.crt → spawn 时经 `NODE_EXTRA_CA_CERTS` 注入路径 →
Electron main 读 ca.crt 建指纹 → (a) `certificate-error` pinning 覆盖 renderer 的
`fetch`（`rtc.js` 控制面调用），(b) `NODE_EXTRA_CA_CERTS` 让 main 进程 Node
`https`/undici `fetch` 也信任该 CA（纵深防御 + 未来 main 进程 https）。

关键澄清（team-lead 疑问的准确回答）：`NODE_EXTRA_CA_CERTS` **只作用于 Node 的
`tls`/`https`/undici fetch（main 进程）**；`rtc.js` 的 `fetch` 跑在 renderer 进程
（Chromium 网络栈），**不受该环境变量影响**。因此 sidecar 必须两者都做：
`NODE_EXTRA_CA_CERTS`（Node 侧）+ `certificate-error` pinning（Chromium 侧）。

边界情况：

- `ca.crt` 缺失/不可读 → main 记 FATAL 并 `fatalMain()`，fail-closed，不降级为无条件接受。
- `server.crt` 轮换（同 CA 重签）→ 无感知。
- CA 整体替换 → 替换打包的 ca.crt → 下次启动指纹重算 → 零代码。
- `--role=phone` 测试模拟器同走 signUrl https，同一 handler 覆盖，无需特判。

### A2 pet-ui（Tauri v2 + React + WebView2）

关键结论（team-lead 疑问的准确回答）：**WebView2 前端 JS 无法从 JS 侧注入自定义 CA
信任**；WebView2（Chromium）在 Windows 走系统根证书库验证。可行路径只有两条：
(a) 把 ca.crt 装进 Windows 受信根库（OS 级信任）；(b) 走 Rust 侧代理（reqwest/tungstenite
带自定义 CA，经 `invoke`/事件转发给 JS）。**选 (a)**；(b) 是过度设计，违反 ADR-017
「Rust 不承载控制面 WS/网络」的最小责任边界。

改动点：

1. `pet-ui/src/state/wsClient.ts:18`：`WS_URL` `"ws://127.0.0.1:8000/ws/pet"` →
   `"wss://127.0.0.1:8000/ws/pet"`。
2. `pet-ui/src/components/Settings.tsx:32`：`PUSH_API`
   `"http://127.0.0.1:8000/api/v1/control/test-push"` →
   `"https://127.0.0.1:8000/api/v1/control/test-push"`。
3. 新增 Rust 模块 `pet-ui/src-tauri/src/ca_trust.rs`（Windows 版
   `ca_trust_windows.rs`）：setup 阶段幂等安装 `resource_dir/certs/ca.crt` 到
   **当前用户**「受信任的根证书颁发机构」库（`CertAddCertificateContextToStore` +
   `CERT_SYSTEM_STORE_CURRENT_USER` + `CERT_STORE_ADD_REPLACE_EXISTING`），按 thumbprint
   判重；把 thumbprint 写入注册表（HKCU `Software\JaxPet\ca_thumbprint`）供卸载清理。
   当前用户库不要求管理员权限。
4. 卸载清理（联动阶段 D4）：按记录的 thumbprint `CertDeleteCertificateFromStore`
   从当前用户根库删除。

信任链：安装时把 ca.crt 装进当前用户 Windows 根库 → WebView2 校验 wss/https 时在
OS 库命中 CA → 真实链验证通过。JS 侧零证书代码。

边界情况：

- 同 thumbprint 已存在 → 幂等跳过。
- 安装失败（极端策略限制）→ 记日志，UI 连接失败且「连接状态可感知」（AC-20），
  不静默降级明文。
- 换正式 CA → 替换打包 ca.crt + 重装 → 新 thumbprint 入根库、按旧 thumbprint 删除 →
  零代码。
- 绝不写 `--ignore-certificate-errors` 或 `additionalBrowserArgs` 关校验。

### A3 Android（Kotlin）

关键发现（回传 team-lead 的事实校正）：当前 post-TRTC 架构下，Android 的
`session_base_url` 指向 CloudBase 云函数（公网 HTTPS、正式 CA），注册
(`/api/v1/voice/devices/register`) 与签发 (`/api/v1/voice/session`) 都走同一
`{session_base_url}`；ADR-012 已删除 LAN 直连，**Android 当前数据路径不连本地自签后端**。
故严格说 Android 当前不需要自签 CA。A3 按「低成本硬化 + 向前兼容」落地，覆盖两种价值：
默认 https + 未来若直连本地后端（`https://<pc>:8000`）时信任链已就位。

改动点：

1. 新建 `mobile-app/app/src/main/res/xml/network_security_config.xml`：
   `<base-config cleartextTrafficPermitted="false">` 下 `<trust-anchors>` 同时声明
   `<certificates src="system" />` 与 `<certificates src="@raw/ca" />`。
   **必须保留 `system` 源**，否则会覆盖系统信任、导致云函数正式 CA 校验失败。
2. 新建 `mobile-app/app/src/main/res/raw/ca.crt`：从 `certs/ca.crt` 拷贝；在构建脚本/
   README 固化一条拷贝步骤，避免手工拷贝漂移。
3. `mobile-app/app/src/main/AndroidManifest.xml`：`<application>` 新增
   `android:networkSecurityConfig="@xml/network_security_config"`。
4. `mobile-app/.../net/VoiceSessionApi.kt` 与 `DeviceRegistrationApi.kt`：URL 校验从
   `startsWith("http://") || startsWith("https://")` 收紧为仅 `https://`
   （云函数域名本即 https），拒绝明文。
5. `strings.xml` `settings_session_hint` 已是 `https://<云函数域名>`，不改。

信任链：OkHttp 默认走系统 `TrustManagerFactory`，`networkSecurityConfig` 的
`<trust-anchors>` 会注入系统信任源 → OkHttp 自动信任 ca.crt 签的证书，真实验证。

边界情况：

- 用 `<trust-anchors>`（信任 CA 根）而非 `<pin-set>`（钉叶证书）：`server.crt` 轮换
  不影响；换 CA 只需替换 `raw/ca.crt` 重新打包，零代码。
- 若未来 Android 直连本地后端，仅需把 `session_base_url` 指向 `https://<pc>:8000`，
  配置已就位。

## Options considered

| 维度 | OS 根库安装（Windows 端采用） | 每进程自管信任（证书链 pinning / NODE_EXTRA_CA_CERTS） | 关闭校验 |
|---|---|---|---|
| 是否真实验证 | 是（链验证到 ca.crt） | 是 | 否 |
| 覆盖 WebView2（pet-ui） | 是 | **否（JS 无法注入）** | 是 |
| 覆盖 Electron renderer fetch（sidecar） | 是（Chromium 走 OS 库） | 需 `certificate-error` pinning | 是 |
| 覆盖 Node https（sidecar main） | 否（Node 默认不看 OS 库） | 是（`NODE_EXTRA_CA_CERTS`） | 是 |
| 卸载清理 | 需按 thumbprint 删除 | 无持久状态 | 无 |
| MVP 成本 | 低（一段 Rust 安装/卸载） | 中（各端各写一套） | 低但不可接受 |

结论：Windows 端（pet-ui + sidecar）以 **OS 根库安装为主信任**（覆盖 WebView2 与
Electron renderer），sidecar **叠加 `NODE_EXTRA_CA_CERTS` + `certificate-error`
pinning 作纵深防御**（覆盖 Node https 与显式 pinning）。Android 用
`network_security_config` `<trust-anchors>`。三条路径全部是「验证到 ca.crt」，无一处关校验。

## Consequences

正面后果：

- 四端 HTTPS/WSS，真实链验证到单一 ca.crt 信任锚，满足「信任优先于便利」。
- 换正式 CA 只需替换各端 ca.crt，零代码改动（thumbprint/指纹均运行时读取）。
- sidecar 的 `certificate-error` pinning 比单纯信任 OS 库更强（显式钉 CA），
  且 `NODE_EXTRA_CA_CERTS` 同时覆盖未来 main 进程 https。
- rtc_bridge（19092）保持 loopback 明文，不扩大改造面，边界显式记录。

负面后果：

- 把自签 CA 装进当前用户根库是一次受信面扩张（同机同用户所有程序都会信任该 CA），
  卸载必须干净移除（D4 联动），否则残留永久信任锚。CA 私钥 `ca.key` 的保管因此是红线。
- sidecar 需要在 `certificate-error` 与 OS 库两条路径上同时正确，测试矩阵增大。
- Android 若未来真的直连本地后端，需要把 `ca.crt` 随 APK 分发并走 `networkSecurityConfig`，
  本 ADR 已预留但未在现网触发。

## Migration and rollback

- 明文 → TLS：后端已 fail-closed（生产必须 TLS）；三端切 https/wss + 装 CA 后实测
  `openssl s_client` / 浏览器 / OkHttp 均验证通过。
- 换正式 CA：替换 `certs/ca.crt` 及三端分发副本 → 各端重读；Windows 端按旧 thumbprint
  删旧、装新；Android 重新打包。零代码。
- 回滚：Windows 端按 thumbprint 从根库删除 CA、移除 `NODE_EXTRA_CA_CERTS` 注入、
  `signUrl`/`WS_URL`/`PUSH_API` 回退 http/ws（仅限本地开发态）；Android 移除
  `networkSecurityConfig` 与 `raw/ca.crt`。禁止回滚到「关校验」后仍宣称满足本 ADR。

## Explicitly not doing

- 不做 `verify=false`、`rejectUnauthorized:false`、`--ignore-certificate-errors`、
  `additionalBrowserArgs` 关校验、`certificate-error` 无条件 `callback(true)`。
- 不为 pet-ui 做 Rust 侧控制面代理（违反 ADR-017 最小责任，且是过度设计）。
- 不把 `ca.key` 分发到任何端；`ca.key` 只留在 `certs/`（gitignore），仅证书签发者持有。
- 不把 rtc_bridge（19092）纳入 TLS（loopback-only 内部音频桥）；仅在它绑定非 loopback 时另立决策。
- 不在源码硬编码 CA/服务端证书指纹或公钥常量（锚内容一律运行时读文件）。

## Design-discipline references

- `references/01-standards/spec-as-contract.md`：以 `docs/commercial-foundation-blueprint.md`
  阶段 A 验收为契约，逐文件点名改动点与 out-of-scope，不凭空新增机制。
- `references/01-standards/context-engineering.md`：校正 team-lead 两个前提——(1) ADR 编号
  018 已被占用；(2) Android 当前数据路径不连本地自签后端——把事实校正写进决策而非沉默迁就。
- `references/01-standards/generated-code-failure-modes.md`：把「`NODE_EXTRA_CA_CERTS` 能修
  renderer fetch」视为会沉默出错的常见误判，通过区分 Node 栈 / Chromium 栈两个验证层阻断复发。

## Related ADRs

ADR-014（voice 安全 fail-closed）、ADR-017（Tauri externalBin 只监督 sidecar）、
ADR-018（本地最小隐私数据，SQLite）、ADR-019（sidecar credential CM 存储）。
