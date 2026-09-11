# 全仓审查报告（2026-09-11）

审查范围：贾克斯·星核 / jax-pet —— 架构、脚本、云端资源、验证证据、安全面、已知风险。
审查口径：**只认机械证据**（命令输出、线上端点实测、测试结果），推断一律标注。

---

## 1. 一句话结论

**控制面与音频对端都已在云端跑通并自证；本地运行态脚本已完成退役。**
但**真机端到端尚未验证**（ADB 通道不可用、手机 App 仍指向旧网关），
且本次审查期间发生**两次目录级文件丢失事件**（已从 git 完整恢复）——
这两件事决定了当前状态仍是 **NO-GO**。

---

## 2. 云端资源实况（CLI 实测）

| 服务 | 类型 | 状态 | 说明 |
|---|---|---|---|
| `jax-voice-api` | container | normal | 控制面：配对/注册/会话签发 + PostgreSQL（生产模式，存储探针全绿） |
| `jax-voice-bridge` | container | normal | 音频对端：云端 sidecar + rtc_bridge（两子进程存活、rtc_bridge health ok、SDK 13.4.802-beta.3） |
| `jax-backend` | container | normal | **遗留**：由未跟踪的 `deploy/backend` 构建（2026-09-08），与新控制面重叠、来源不受版本控制 |
| `jax-relay` | container | normal | 遗留中继服务（2026-08-14），未审查是否仍被使用 |

数据库：CloudBase PostgreSQL `REPLACE_WITH_PG_INSTANCE_ID`（ap-shanghai，私网 `REPLACE_WITH_PG_PRIVATE_IP:5432`，VPC `REPLACE_WITH_VPC_ID` / `REPLACE_WITH_SUBNET_ID`），
已应用 migration `20260908154838_voice_control_plane`（17 表 / 50 索引 / 8 FK / 28 CHECK），应用账号 `jax_voice_app`（仅 DML，`anon`/`authenticated` 已 REVOKE）。

---

## 3. 本地运行态：已退役什么、还剩什么

**已退役（commit `eda231f`，27 个文件）**：watchdog / 计划任务注册 / 服务管理 / 本地栈启动 / adb 保活
及其专属契约测试，外加只服务于 watchdog 的 Rust 包装器 `tools/jax-watchdog-wrap`。
同时作废了一处未提交的本地绕行（backend 绑 `0.0.0.0` 让手机经 Tailscale 直连）。

**仍保留（属构建/测试/诊断，不是"用脚本解决产品问题"）**：`scripts/` 现有 28 项，主要是
`poc_*` 探针、`download_*`（模型下载）、`setup_env.ps1`、`device-acceptance-capture.ps1`（真机取证）、
`cleanup-*.py`、`scripts/lib/sidecar-*.js`（sidecar 打包工具链，有独立测试）、`release_governance/*`。

**机器侧残留（未处理）**：本机可能仍注册着 `Jax-Watchdog-AtStartup` / `Jax-Watchdog-Every5Min`
两个计划任务，指向已删除的脚本 → 会持续失败。卸载命令见 `docs/retirements/local-runtime-scripts-2026-09-11.md` 第 4 节。

---

## 4. 验证证据等级（诚实分级）

| 项 | 证据 | 等级 |
|---|---|---|
| 控制面 ↔ PostgreSQL | `/cloud/status` 存储分步探针 `read/write/nonce/limit/pairing` 全绿 + `/cloud/selfcheck` 全绿（由服务自身对真库执行） | **真库实证** |
| 控制面写入落库 | 配对码经公网创建 → 真库 `pairing_codes.code_hash` 与本地 sha256 逐字节相符（MATCH: True） | **真库实证** |
| 云端音频对端可运行 | `/bridge/status` `ok=true`，两子进程 alive、rtc_bridge health ok、SDK 版本已装载 | **云端进程实证** |
| 音频通路（TRTC 进出、播放、打断） | 无 | **空白** |
| 手机 ↔ 云端端到端 | 无（ADB 不可用；App 仍指旧网关） | **空白** |
| 代码质量 | `contract+unit+integration` **1047 passed / 0 failed** | **单元/契约级** |
| PG 适配（方言/事务/行锁） | 契约测试 + fake pool；真库仅覆盖控制面读写路径 | **部分真库** |

---

## 5. 安全面

- **凭据**：仓库与 CI 均不含明文；DB/TRTC/Qwen/cipher key 只在 CloudRun 服务配置（远端）。
  本次会话中曾被临时写入本地 `.env` 与 `tmp/`，**已全部清除**（`.env` 仅剩非生产项）。
- **TLS 固定**：sidecar 以 `NODE_EXTRA_CA_CERTS` 做密码学级 pinning，锚为签发控制面 leaf 的中间 CA
  （DigiCert Secure Site OV G2 TLS … CA1，`certs/cloud-control-plane-issuer.pem`，2032-12-14 到期）。
- **数据库权限**：应用角色仅 DML；用户角色被 REVOKE；RLS 未启用（当前由应用层守卫）。
- **待办**：`userSig` 已版本化加密（commit `d305e0b`）；但**密钥轮转编排未做**，`VOICE_USER_SIG_CIPHER_KEY` 轮换无流程。

---

## 6. 风险登记（按优先级）

| # | 风险 | 影响 | 证据/现状 |
|---|---|---|---|
| R1 | **真机端到端为零** | 三项语音 KPI（流畅度/完整度/打断）无法判定；产品不可发布 | ADB transport 不可用；实测记录见历史日志 |
| R2 | **手机 App 仍指向旧网关** `VoiceConfig.kt:150` | 即使云端就绪，产品也没用上新控制面 | 该域名实测 `/health` 404、`/cloud/status` 400 |
| R3 | **审查期间两次目录级文件丢失**（`scripts/`、`backend/tests/contract/`） | 有真实数据丢失风险，且发生在删除操作之后 | 178 个受跟踪文件曾在磁盘消失，已用 `git checkout` 完整恢复（0 意外缺失）；未跟踪文件损失 1 个（已重建） |
| R4 | **CI 流水线尚未真正运行** | 部署仍只能靠手工 CLI（本次就是这样做的） | 需 GitHub 侧配 secrets/vars |
| R5 | `jax-backend` 由未跟踪的 `deploy/backend` 构建 | 生产运行不受版本控制代码；与新控制面职责重叠 | `deploy/backend/cloudbaserc.json` mtime 与服务 UpdateTime 吻合 |
| R6 | 音频桥为**单会话模型** | 多用户并发即互相顶替 | `backend/rtc_bridge/server.py:53-57`（新连接顶替旧连接） |
| R7 | 桥服务 `MinNum=1 / MaxNum=1`，无多实例一致性验证 | 单点；且长连接服务在 CloudRun 的回收行为未实测 | 服务配置实测 |
| R8 | MCP 源码部署会清掉 `VpcConf` | 每次用 MCP 部署都要补 VPC，易漏 | 多次实测 `verifiedAfterDeploy.hasVpcConf=false` |
| R9 | 桌面端（Tauri）仍自带本地监督（`o020_controller*`/`watchdog.rs`） | 与"完全走云端"目标存在战略冲突，未决策 | 源码存在，未审查其可退役性 |
| R10 | 仓库脏：132 项未跟踪、12 个未提交改动 | 干扰审计与打包；部分为历史 worker 产物 | `git status --porcelain` 统计 |

---

## 7. 建议的下一步（按依赖顺序）

1. **恢复真机通道并完成一次端到端**：手机进 TRTC 房间 ↔ 云端 `jax-voice-bridge`，
   用 App 自身的状态与日志作证据；这是解开 R1 的唯一途径。
2. **把 App 指向新控制面**（R2）：改 `VoiceConfig.kt` → 重打包 → 真机验证
   （与第 1 步同一轮做，避免两次真机占用）。
3. **清理机器侧计划任务**（第 3 节命令），完成"本地运行态"闭环。
4. **让 CI 真正跑起来**（R4）：配 secrets/vars，然后**首次用流水线部署**，替代手工 CLI。
5. **处置 `jax-backend` 与 `deploy/backend`**（R5）：确认其线上用途后，改为受跟踪来源或下线。
6. **多用户/多实例**（R6/R7）：`BridgeServer` 按会话隔离重构 + 双实例与长连接回收实测。
7. **安全收尾**：cipher 密钥轮转编排；评估 PG RLS 是否需对应用层守卫补强。
8. **决策桌面对端去向**（R9）：若产品确定"纯云端"，桌面端的本地监督与打包链应进入退役计划。

---

## 8. 本次审查的边界（未做的）

未执行：真机操作、Remote DDL/迁移、生产流量切换、`jax-backend`/`jax-relay` 的变更、桌面端代码审查、
成本评审、备份/PITR 演练。以上均需单独授权或设备可用。
