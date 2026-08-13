# OPEN-DECISIONS — 悬而未决决策登记册

> 规范：只追加 + 就地 RESOLVED；每次开工前复现未决项；解决后升格为 ADR。
> 状态：2026-08-03 M0 快照

---

## 未决项

### O-001 全双工语音的 MVP 归属
- 类别：product-scope
- 描述：全双工语音是否进 MVP（V1.1 已定，但若 PoC B3 通过，是否提前并入 V1 开发序）
- 影响：版本节奏与验收范围
- 备选：A) 保持 V1.1；B) PoC 通过后并入 V1 后段
- Resolves when：M1 PoC B3 结果出炉后由项目总监裁决

### O-002 手机推送通道最终选型（2026-08-03 更新：企微→飞书）
- 类别：technical
- 描述：用户手机系统未知；MVP 实现企业微信 + ntfy 双 Provider，是否默认启用企微（需用户提供 webhook）
- 更新：用户确认 **无企微、有飞书、有微信** → 选型改为 **飞书机器人（替代企微）**；微信个人号无官方 webhook 不适用；ntfy 保留为备选
- 影响：推送可达性验证；飞书机器人同时承载"手机语音对话"近期路径（O-014）
- 备选：A) 飞书 webhook 文本推送（近期）+ B) 飞书机器人语音消息双向（O-014 路径）
- Resolves when：用户创建飞书自建应用提供 App ID/Secret 后配置实测
- 类别：technical
- 描述：用户手机系统未知；MVP 实现企业微信 + ntfy 双 Provider，是否默认启用企微（需用户提供 webhook）
- 影响：推送可达性验证
- 备选：A) 企微默认 + ntfy 备选（推荐）；B) 仅 ntfy；C) 加 Bark（iOS）
- Resolves when：用户提供 webhook 或明确手机系统

### O-003 语音唤醒方式
- 类别：product-scope
- 描述：进入 Listening 的方式：点击宠物 / 全局热键 / 唤醒词（唤醒词需额外模型推理）
- 影响：V1.1 交互细节
- 备选：A) 点击宠物 + 全局热键（推荐，零额外推理）；B) 加唤醒词（轻量模型，约 200MB）
- Resolves when：V1.1 开发排期确定

### O-004 被监控应用窗口匹配策略
- 类别：technical
- 描述：三 App 的窗口标题/进程名匹配规则（Codex 终端窗口标题变化、Trae 多窗口），匹配失败时的降级
- 影响：监控稳定性
- 备选：A) 进程名主匹配 + 标题正则（推荐）；B) 用户手动选择窗口（WGC 选择器）
- Resolves when：PoC B2 实测三窗口标题规律后定

### O-005 语音播报 TTS 归属（2026-08-03 审计追加）
- 类别：product-scope
- 描述：PRD §5.1.2 四级提醒含"语音播报"，但 SPEC §1 V1 未列且 voice/ 为空 → V1 是否依赖 TTS 边界不清
- 影响：V1 验收范围
- 备选：A) V1 四级提醒仅"动效+推送"，语音归 V1.1（推荐）；B) V1 引入 edge-tts 轻量播报
- Resolves when：PoC B3 结果 + 用户裁决

### O-006 推送内容隐私边界/脱敏（2026-08-03 审计追加）
- 类别：product-scope
- 描述：PRD 主打"本地隐私"，但推送默认经 ntfy.sh（云端）发送文本+截图出本机，唯一穿透点未声明
- 影响：安全边界与 PRD 承诺一致性
- 备选：A) 企业微信 webhook + 脱敏文本（不含截图、不含敏感代码片段）——**用户已裁决采用**；B) 仅本地提醒不推送
- Resolves when：✅ 已裁决（webhook URL 待用户提供后配置实测）

### O-007 P1 报告/建议的产品形态（2026-08-03 审计追加）
- 类别：product-scope
- 描述：advice_generator 产出的优化建议呈现位置（桌宠气泡/面板/推送/语音）未定义
- 影响：V1.1/V1.2 交互
- 备选：A) 面板时间线展示 + 4 级提醒附带（推荐）；B) 独立报告页
- Resolves when：V1.1 排期确定

### O-008 监控目标扩展（2026-08-03 用户裁决）
- 类别：product-scope
- 描述：用户确认桌面 4 目标在线均需监控：Codex（开源桌面版重点，CLI 未装）+ Trae + Hermes + WorkBuddy。实测进程名：codex.exe / TRAE SOLO CN.exe（原配置 trae.exe 匹配失败）/ Hermes.exe / WorkBuddy.exe
- 影响：monitors.yaml 配置 + B2 窗口匹配校准 + 轮询预算（4 目标 × 6-8s，模型单实例串行）
- 备选：已裁决 4 目标全保留；多进程（Trae 8/WorkBuddy 8/Hermes 6）需标题正则精确匹配
- Resolves when：✅ 已裁决；标题正则待 PoC B2 实测校准

### O-009 D-3 四级递进语义（2026-08-03 审计追加）
- 类别：product-scope
- 描述：PRD §6.2 D-3 "四级渐进打扰递进"语义歧义——"递进"指按严重度定级还是按时间累进？审计发现前端仅二元化实现（level 1/2 与 3/4 同渲染），缺分级依据。
- 影响：提醒分级与桌宠 UI 表现
- 备选：A) 按严重度定级（推荐）：stuck 超时=4 级、off_track=3 级、恢复=1 级；B) 按时间累进（低→高逐级升级）
- Resolves when：✅ 已裁决 A（按严重度定级，已写入 PRD §6.2 D-3 EARS 描述）

### O-010 HomeRail 开源项目参考评估（2026-08-03 用户提出）
- 类别：design-decision-to-evaluate
- 描述：用户建议调研开源项目 HomeRail（github.com/xiaotianfotos/homerail，MIT，2026-07 发布，TypeScript 语音优先 DAG 工作流运行时，本地 homelab/NAS 部署），评估能否复用减少重复开发
- 总监调研结论（2026-08-03）：
  - **理念可借鉴**：① 语音面契约（ASR/TTS/VAD 契约化设计）→ V1.1 语音管线参考；② DAG 显式交接/运行可重放/评分卡 → 我们 EventBus 流水线可补"判定留痕可回放"；③ "注意力稀缺→打扰最小化"与四级渐进打扰设计哲学互相印证
  - **不可复用（技术不重叠）**：① TS 运行时 vs 我们 Python FastAPI——无法嵌入；② 依赖 Docker Worker + Claude Agent SDK endpoint——我们核心是本地 llama.cpp-omni + WGC 屏幕监控；③ HomeRail 无屏幕监控/视觉判定/桌面宠物/渐进打扰——我们核心功能它完全没有，不存在"重复开发"
  - **结论倾向**：不引入为技术依赖（增加 TS+Docker 复杂度），语音面契约设计与可观测理念在 V1.1 时参考
- Resolves when：✅ 已裁决（ADR-009：保留 Python 监控后端、借鉴设计理念不引依赖；语音面契约 V1.1 对照）

---

## 已解决（升格为 ADR）

| ID | 摘要 | 升格 |
|---|---|---|
| O-000 | 推理引擎选型 | ADR-001 |
| O-000 | 窗口截屏方案 | ADR-002 |
| O-000 | 语音管线 | ADR-003 |
| O-000 | 桌宠技术栈 | ADR-004 |
| O-000 | 推送插件 | ADR-005 |
| O-000 | 后台架构 | ADR-006 |
| O-000 | 宠物视觉 | ADR-007 |
| O-000 | 监控策略 | ADR-008 |
| O-010 | HomeRail 开源项目评估 | ADR-009 |

### O-011 混合大脑架构（2026-08-03 用户裁决）
- 类别：technical
- 描述：任务拆解/评审/指令生成用哪个模型
- 决策：✅ **本地 9B（MiniCPM-o，监控/视觉/轻量）+ DeepSeek V4 Flash 正式版 API（拆解/评审/指令生成）混合**——用户要求"省钱到极致"；隐私：仅会话摘要上传云端，截图不出本机（延续 O-006）
- Resolves when：✅ 已裁决，V1.5 落地（DeepSeek 客户端 + 混合路由）

### O-012 指令注入方式（2026-08-03 待补裁）
- 类别：product-scope
- 描述：贾克斯生成的指令如何进入 Codex（全自动键鼠注入 / 确认后注入 / 仅生成文本）
- 决策：默认"**确认后注入**"（生成→用户确认→自动注入 Codex 输入框）起步，全自动留 V2——待用户最终确认
- Resolves when：V1.5 开发排期前用户确认

### O-013 安全边界重定义：受控注入（2026-08-03 用户裁决）
- 类别：product-scope
- 描述：原 PRD "只监控+提醒+建议，不操控" 升级
- 决策：✅ **受控注入**——注入前用户确认；注入内容仅指令文本（不读 Codex 内部数据/不键鼠模拟 UI 之外的操控）；截屏不出本机；上传云端仅会话摘要且脱敏
- Resolves when：✅ 已裁决；PM 同步更新 PRD §5.4


### O-014 手机语音对话（类 Siri/GPT-Live，2026-08-03 用户核心需求）
- 类别：product-scope
- 描述：用户核心诉求 = 手机跟贾克斯语音对话（唤醒→说话→语音回答），对标三星 Bixby/苹果 Siri/GPT-Live 体验。文字推送只是辅助，语音双向交互是主形态
- 近期路径（V1.5 增强）：**飞书机器人**——手机飞书发语音消息 → 事件订阅转发电脑贾克斯 → 本地 ASR（模型原生/sherpa-onnx）→ DeepSeek 大脑处理 → TTS 生成语音 → 上传回传飞书语音消息。**飞书即手机端 UI，无需自研 App**
- 远期路径（V2）：自研手机端（小程序/App）+ WebSocket 云端中继 → 实时流式语音 + 打断（完整 GPT-Live）
- Resolves when：用户创建飞书自建应用（提供 App ID/Secret）后实施

### O-015 语音形态红线：最终=本地模型原生全双工（GPT-Live 级）（2026-08-05 用户裁决）
- 类别：product-scope
- 描述：用户三条硬性要求：①不要 ASR 假语音来回制 ②不要不能迭代的替补方案 ③最终必须达到真正 GPT-Live 效果（流式双向+随时打断），不是"我说一句他回一句"
- 裁决：✅ **M3 全双工 = 唯一终点**（本地 llama-omni 原生 APM：流式 ASR+流式 TTS+实时打断 barge-in，mobile-voice-spec §8 apm_bridge + §4.4）；当前半双工（sherpa STT→大脑→edge-tts）仅为 M2 过渡调试链路，**不作为最终交付形态**；任何"不能迭代的替补"直接否决
- 落地路径：PoC B3（本地模型原生全双工 APM 验证）→ apm_bridge 实装 → App 端 barge-in（silero-vad 双门限）→ 全双工替换半双工
- Resolves when：M3 交付验收

### O-016 CloudBase HTTP 访问服务自定义域名备案（2026-08-06 追加）
- 类别：waiting-on-external-condition
- 描述：trtc-sign 云函数走 CloudBase HTTP 访问服务**默认域名**（`https://jinhong-d2g55ycl591208475.ap-shanghai.app.tcloudbase.com/...`），官方定位开发/测试形态（频率限制、部分高级能力不可用、浏览器直访有安全提示中间页）。MVP 阶段手机 App 直调可接受（ADR-012 O1 裁决 ✅）；**生产上线前须绑定已备案自定义域名**，以获得完整服务能力与稳定性保障
- 影响：生产环境 URL 形态、云函数访问稳定性、频率限额
- 备选：A) 保持默认域名（MVP 已接受）；B) 绑定已备案自定义域名（需 ICP 备案 + SSL 证书，CNAME 接入云开发 CDN）
- Resolves when：用户提供已备案域名 + 证书后配置实测（外部条件）

### O-017 StarletteDeprecationWarning 测试栈治理（2026-08-07 Phase 3 Batch 1 追加）
- 类别：design-decision-to-evaluate
- 描述：fastapi.testclient 导入 starlette.testclient 时触发 `StarletteDeprecationWarning: install httpx2 instead`；来源是 fastapi 0.141.1 / starlette 上游迁移期，非本项目测试代码。QA 要求不 suppress、通过精确依赖集治理
- 机械证据：`pip check` 无破损；`pip install --dry-run -r requirements.txt` exit 0；`npm ci --dry-run` exit 0（60 包干净可解析）；依赖声明已全部精确锁定
- 备选：A) 升级 fastapi/starlette 到支持 httpx2 的稳定组合（当前无稳定版，风险高）；B) 保持精确锁定 + 记录已知上游限制，Task 14 干净构建时复查（推荐）；C) 全部测试改用 httpx.AsyncClient + ASGITransport 绕过 TestClient（改动面大，Batch 2 后评估）
- Resolves when：Task 14 干净构建门禁复查；或上游发布稳定 httpx2 支持后由架构师裁决

### O-018 Windows sidecar fresh-install credential provision（2026-08-08 Task 19 追加）
- 类别：waiting-on-delivery-capability
- 描述：仓库和OpenAPI当前没有sidecar credential bootstrap/claim能力，也没有安装器custom action或最终用户上下文provisioner。商业P0 fresh install无法安全把后端同值opaque credential写入当前用户Credential Manager。
- 2026-08-09 recovery审计证据：`tauri.conf.json`只声明标准`nsis`/`msi`与`externalBin`；`build.rs`仅调用`tauri_build::build()`；Cargo没有`src/bin/provision_sidecar_credential.rs`；仓库没有WiX/NSIS模板、custom action、`CreatePipe`/`CreateNamedPipe`/`DuplicateHandle`或继承handle实现。现有`scripts/setup_env.ps1`只创建venv并复制`.env.example`，`install-scheduled-tasks.ps1`只注册Limited/Interactive计划任务，均不是凭证供给入口。当前不具备可安全按TDD落地的安装上下文，禁止硬写孤立helper。
- 影响：干净安装sidecar保持fail-closed，不能完成首次受保护pending请求，商业发布保持FAIL。
- 最小可实施发布切片：新增真实安装编排与一次性`provision_sidecar_credential.exe`；安装器在受保护部署边界生成至少32 bytes CSPRNG opaque值并同步写后端受保护配置；以最终交互用户token启动helper，只经不落盘匿名pipe或显式继承handle传入；DACL只授予该用户SID与SYSTEM，禁止通配Everyone/Users，父端仅继承指定handle；helper只返回`0`或稳定非零码，不回显，完成后关闭handle并RAII零化；安装器失败即不启动sidecar并回滚新安装版本/保持上一版本。
- 无泄露E2E门禁：干净Windows用户执行安装；用Process Explorer/Process Monitor或等价证据确认argv、父全局env、普通文件、注册表、安装包、WebView IPC和日志均无测试secret；确认helper token SID等于最终用户，Credential Manager三槽ACL与mutex DACL仅目标用户/SYSTEM；重登后active可读；用同值真实调用`GET /api/v1/voice/session/pending`并得到受保护响应；卸载/失败回滚后helper、pipe/handle和临时内存无残留。不得用手工`CredWriteW`、`.env`或README步骤替代。
- 成本边界：沿用Spec锁定的Tauri+React桌面栈，不因CloudBase/Docker/Vercel/Railway部署优先级改变本地Windows installer；不部署、不新增云平台能力。一次性工程量预计3-5人日（安装编排/用户token/pipe DACL 1.5-2.5，helper与零化0.5-1，干净机与跨用户E2E 1-1.5）；代码签名证书、Windows测试机/VM与后端受保护secret配置为额外外部成本。
- 条件推荐：受保护安装/部署通道同时写后端受保护secret setting，并通过目标用户与父安装进程限权的匿名pipe/继承handle把同一值交给最终交互用户上下文provisioner。禁止argv、普通文件、日志、WebView IPC、父进程全局环境或安装包内嵌secret。
- 拒绝：复用Android pairing/register；现有OpenAPI的`platform`仅android，credential主体与撤销语义不同。未来sidecar claim须按Spec大改流程另立任务。
- 2026-08-10 输入就绪核验（只读，不实施）：后端受保护配置字段已存在（`backend/app/config.py` 的 `voice_sidecar_credential` / `_next` / `_next_enabled_at` / `_next_expires_at` / `_config_revision`）；Windows 三槽 Credential Manager backend 已实现并通过独立 QA（`credential_windows.rs` + Win32 backend + SID hash named mutex）；NSIS 安装包已构建（`outputs/JaxPet-Setup-x86_64-20260810.exe`，94,757,822 B）。仍缺：`src/bin/provision_sidecar_credential.rs`、NSIS custom action 编排、限权匿名 pipe/继承 handle 通道、代码签名证书与干净 Windows 机 E2E。按契约 §6.1"缺任一证据继续 blocking"与"禁止硬写孤立 helper"边界，本轮不实施孤立 provisioner。
- 2026-08-10 16:45 切片 1 完成（实现侧）：`pet-ui/src-tauri/src/bin/provision_sidecar_credential.rs`（153 行）已按 §4/§6.1 落地：继承 stdin 匿名管道读一次性 secret（Zeroizing、限长 512）→ `SecretString::parse_utf8` validate → revoke 幂等清理三槽 → provision active 写入+readback → ExitCode 0/1/2 不回显。单测 7/7、release 编译 exit 0、真实 CM 冒烟（active 恰 1 条、staging/backup absent、负向 exit 1、二次幂等、清理 0 残留）。独立 QA 三遍复验进行中。切片 2（NSIS custom action 编排：CryptGenRandom → CreatePipe → CreateProcess 继承 handle → WriteFile → WaitForSingleObject → 非 0 Abort）因 NSIS System 插件复杂度与宿主干扰风险，且干净机 E2E 属外部条件，暂不冒险产出半成品；切片 2 需单独 TDD/审计后方可放行。
- 2026-08-10 17:00 切片 1 独立 QA 三遍复验 PASS（证据 `o018-slice1-qa-20260810-163808`）：源码契约 15 项、fresh target 独立编译（test 7/7、release exit 0、exe sha256 861dca25...、verifier 门执行通过）、真实 CM 行为复验（正向/负向/幂等/513 超长/清理 0 残留）。后端同值链 PASS：同一 CSPRNG secret 经 provisioner 写 CM 后，后端 `SidecarCredentialHashSet(hash_credential(S))` 下 `verify_sidecar(S)` 通过、错误值 40101、后端错值配置时 CM 同值被拒。仍缺：NSIS custom action 编排（切片 2）、代码签名证书、干净 Windows 机无泄露 E2E。
- Resolves when：installer/custom action、provisioner、pipe ACL/handle inheritance、后端同值配置和干净机无泄露E2E证据全部通过；本次只补机械blocking与实施边界，保持OPEN。

### O-019 Sidecar credential 后端 current/next 轮换窗口（2026-08-08 Task 19 追加）
- 类别：technical
- 描述：后端当前只加载单一 sidecar credential hash；本地 Credential Manager 事务不能保证 replacement 已被服务端接受。无中断轮换需要同一逻辑 credential 的 `current_hash + next_hash` 最长 10 分钟接受窗口和明确收敛/回滚。
- 影响：本地 rotate 即使成功，也不能宣称线上无中断轮换；直接替换服务端单值会使在线或新 child 立即 401。
- Current Leaning：采用 `docs/windows-sidecar-credential-contract.md` §6.3 的内部部署契约，不修改现有 Bearer/OpenAPI。后端以受保护部署 secret 构造不可变 `SidecarCredentialHashSet(current_hash,next_hash?,next_enabled_at?,next_expires_at?,config_revision)`；inactive/scheduled/expired 只常量时间比较 current，active 对 current 与 next 无条件完成两次等长 ASCII `hmac.compare_digest` 后非短路 OR 合并，不记录命中槽；半配置/TTL 超过 600 秒/时钟或 revision 不可判定均 fail-closed。rollout 固定为全实例双值同 revision -> 本机 provision/rotate -> 真实 pending+sign 健康确认 -> next promote current -> 删除旧值；失败按窗口状态回滚或隔离不一致实例。
- 边界：不得借此引入 sidecar device identity、`X-Sidecar-Device-Id`、registration API/数据库、第二 credential store；secret/hash 不得经 API、WebView、argv、日志或普通文件下发。
- Resolves when：后端配置/validator 与测试按 §6.3 落地；compare spy 证明 active 时 current/next 始终各比较一次、current 命中也不短路，inactive/scheduled/expired 不比较 next，且不暴露命中槽；证明 current 与窗口内 next 均通过且主体/nonce/限流不变，next 未启用/恰到期/过期拒绝，配置损坏生产拒绝启动；完成所有实例 revision 一致、双值进入、本机 provision、真实 pending+sign 健康、promote、旧值 401、窗口到期与部分实例失败回滚的机械证据。未满足前保持 OPEN。

### O-020 Windows Credential Manager事务与真机E2E（2026-08-08 Task 19 追加）
- 类别：existing-design-boundary
- 描述：一个逻辑opaque credential采用固定active/staging/backup事务槽，需要进程内mutex、当前用户/SYSTEM限权named mutex、WAIT_ABANDONED恢复、确定性状态表、零化和最坏失败fail-closed。2026-08-09 recovery审计确认当前`credential_windows.rs`仍只有单个`target_name`，`rotate()`直接覆盖active再readback；没有staging/backup常量、进程内/Windows named mutex、SID-hash/DACL、WAIT_ABANDONED恢复或跨进程串行化。
- 影响：rotate验证失败可能丢失旧值；崩溃、并发rotate/revoke、slot损坏和ACL边界未证明前不能放行Windows真机闭环。当前`CredReadW`先复制到普通`Vec<u8>`才解析，且`CredFree`不受RAII guard覆盖，panic/错误路径零化证据不足。
- externalBin发布链恢复进展（2026-08-09）：新增锁文件驱动的`build-sidecar-external-bin.js`与fail-closed verifier；构建从Electron `31.7.7`完整dist新鲜复制目标三元组exe，`resources/app`独立执行`npm ci --omit=dev`，验证TRTC `13.4.802-beta.3`的`.node`、LiteAV/FFmpeg/SoundTouch DLL和media server，生成64位小写SHA-256与provenance，并通过`bundle.resources`把Electron runtime sibling和相对`resources/app`安装到externalBin同目录。manifest现为严格schema闭集：runtime_files是完整runtime path/hash集合，native_files是其安全关键子集且逐项与runtime一致；新增/遗漏/重复/绝对/遍历路径拒绝，APP_SOURCES白名单漂移拒绝。Tauri beforeBuild执行build+verify，Rust release build再verify并将manifest digest编译期嵌入；首次启动和watchdog restart在读credential前复验digest、exe及runtime/native闭集。原115712-byte console PE不再作为输入或证据，生成exe/runtime/hash/provenance均被精确ignore。第三轮源码候选进一步区分`build_input_file`与安装逻辑名`installed_file`，固定五个required-native路径，闭集只枚举专用受管runtime命名空间，不把Tauri主程序/externalBin同级文件纳入；启动入口收敛为同一supervisor实例内validate→provider load→revalidate→私有spawn，并对manifest/exe symlink fail-closed。当前证据仍只是待Cargo编译和installed-layout验证的源码候选，不证明可信TLS完整构建、NSIS/MSI安装、签名、fresh credential供给或Windows干净机运行。
- 边界：staging/backup只是同一credential事务副本，不是第二sidecar identity或第二长期store。安全最小实现必须先补Win32故障注入RED，再实现固定A/S/B槽、双层锁、限权mutex、RAII`CredFree`与`Zeroizing<Vec<u8>>`；但这不替代O-018安装供给，也不替代真实externalBin打包。
- 生产就绪记分卡（2026-08-09运维范围，目标Silver）：测试+回归Bronze（Node打包篡改门与Rust hash单测已补，但无安装/崩溃/跨用户E2E或CI）；契约Silver（Spec/ADR/实现契约明确）；安全Bronze以下（fresh-install、mutex ACL、三槽、零化未实现）；无障碍Bronze（不受本地installer优先级改变，但全项目仍缺E2E）；性能Bronze（无Windows真机/启动预算证据）；可观测Bronze（provenance与稳定错误码已补，安装/轮换审计未闭环）；发布安全Bronze（externalBin可重复构建/hash门已补，但无真实installer/custom action、签名与回滚演练）。总档Bronze以下，取最低档，未达商业Silver。
- Resolves when：stage/backup/promote/verify/cleanup逐点崩溃注入、双进程并发、WAIT_ABANDONED、A/S/B损坏/拒读/拒删、跨用户ACL、relogin persistence与泄露扫描在Windows真机通过；真实Node/Electron externalBin、校验hash与NSIS/MSI产物由可重复构建链生成并完成安装/回滚验收。本次保持OPEN。
