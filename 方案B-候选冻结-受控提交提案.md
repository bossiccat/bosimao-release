# 方案 B｜候选冻结与受控提交提案

## 提案目的

把当前方案 B 工作树产物变成一个可追溯的候选提交，为之后的证据归档和真机验收建立固定 SHA。

**本提案不执行 `git add`、不创建提交、不删除或覆盖任何文件。**

## 当前前提

- 方案 B 当前存在已修改及未跟踪交付物，不能以现有 HEAD 直接作为候选。
- 商业发布状态仍为 **NO-GO**；形成候选 SHA 只解决可追溯性，不代表真机或发布门禁通过。
- 必须先逐项审查差异，不得使用 `git add .` 或 `git add -A`。

## 建议的候选范围

### A. 必须成组纳入

#### 1. 后端运行时代码

- `backend/app/api/routes_voice_hello.py`
- `backend/app/api/routes_voice_security_context.py`
- `backend/app/api/routes_voice_sessions.py`
- `backend/app/api/routes_voice_status_stream.py`
- `backend/app/api/routes_voice_stream.py`
- `backend/app/api/routes_voice_termination.py`
- `backend/app/api/routes_voice_wake.py`
- `backend/app/api/voice_termination_contract.py`
- `backend/app/api/voice_termination_guard.py`
- `backend/app/voice/control_plane.py`
- `backend/app/voice/control_plane_ack.py`
- `backend/app/voice/control_plane_base.py`
- `backend/app/voice/control_plane_kws.py`
- `backend/app/voice/control_plane_retry.py`
- `backend/app/voice/control_plane_sessions.py`
- `backend/app/voice/control_plane_wake.py`
- `backend/app/voice/hello_proof.py`
- `backend/app/voice/hello_runtime.py`
- `backend/app/voice/hello_service.py`
- `backend/app/voice/migration_runner.py`
- `backend/app/voice/repositories/hello_proofs.py`
- `backend/app/voice/sidecar_sign_service.py`
- `backend/app/voice/trusted_gateway.py`
- `backend/rtc_bridge/redemption.py`
- `backend/rtc_bridge/ack_reporter.py`
- `backend/rtc_bridge/drain_ack.py`

#### 2. 数据库迁移（必须与运行时代码同批）

- `backend/app/voice/migrations/005_control_plane_ledger.sql`
- `backend/app/voice/migrations/006_wake_events.sql`
- `backend/app/voice/migrations/007_hello_proofs.sql`

#### 3. 同批回归测试

- `backend/tests/contract/test_solution_b_openapi_contract.py`
- `backend/tests/integration/test_voice_hello_redeem.py`
- `backend/tests/integration/test_voice_termination_secured.py`
- `backend/tests/unit/test_control_plane_ledger.py`
- `backend/tests/unit/test_hello_proof.py`
- `backend/tests/unit/test_rtc_bridge_ack_report.py`
- `backend/tests/unit/test_rtc_bridge_apm_ack.py`
- `backend/tests/unit/test_rtc_bridge_ctrl_relay.py`
- `backend/tests/unit/test_rtc_bridge_redemption.py`
- `backend/tests/unit/test_voice_hello_runtime.py`
- `backend/tests/unit/test_voice_termination_routes.py`
- `mobile-app/app/src/test/java/com/jax/voice/net/RtcClientTerminateNoticeTest.kt`
- `mobile-app/app/src/test/java/com/jax/voice/net/VoiceSessionApiTerminateTest.kt`
- `mobile-app/app/src/test/java/com/jax/voice/voice/ExitTerminationFlowTest.kt`
- `sidecar/test/bridge-note-termination.test.js`
- `sidecar/test/rtc-termination-cmd.test.js`

#### 4. 两个跨端消费者

- `mobile-app/app/src/main/java/com/jax/voice/voice/ExitTerminationFlow.kt`
- `sidecar/rtc-termination.js`

#### 5. 依赖锁文件（有条件）

- `uv.lock`：仅当 `pyproject.toml` / `requirements.txt` 的差异确认属于本方案并完成依赖一致性审查时纳入。

## B. 必须逐文件审查后再决定

以下属于已跟踪变更面，可能是方案 B 必需上下游，但不应自动纳入：

- `.gitignore`
- `backend/app/api/routes_voice_secured.py`
- `backend/app/config.py`
- `backend/app/main.py`
- 其余 `backend/app/voice/*` 关联文件
- `backend/rtc_bridge/config.py`
- `backend/rtc_bridge/server.py`
- `backend/rtc_bridge/session.py`
- OpenAPI 与商业语音 API 文档
- `mobile-app/.../VoiceSessionApi.kt`
- `mobile-app/.../RtcClient.kt`
- `mobile-app/.../VoiceForegroundService.kt`
- `sidecar/bridge.js`
- `sidecar/rtc.js`
- `pyproject.toml`
- `requirements.txt`
- 现有测试文件的修改

审查重点：是否为方案 B 控制面与终止链的实际依赖、是否夹带主树或其他任务变更、是否存在测试弱化或配置降级。

## C. 需你确认的范围选择

1. `tools/smoke_termination_chain.py`
   - 建议纳入“候选工具与可复验验收”范围。
   - 它只能证明本地 CP + bridge + sidecar 的 WS ctrl 注入路径；不得作为 Android/TRTC/APM/Brain 真机通过声明。

2. `docs/plans/2026-08-23-solution-b-control-plane-contract-draft.md`
   - 必须确认该文档是正式设计依据还是仅保留草案。

3. `docs/plans/2026-08-25-realdevice-e2e-acceptance.md`
   - 建议纳入；它是后续 AC-1～3、G4/G5/G7 的真机门禁准绳。

4. 现有 `overview.md` 与本轮审计报告
   - 建议作为工作记录保留，但不要把概览中的会话内测试数字当作候选正式证据；正式证据只能来自候选 SHA 后的新鲜归档。

## D. 明确不得纳入

- `tools/_probe_uv.py` 等临时探针；
- `.venv/**`、`__pycache__/**`、`*.pyc`；
- `mobile-app/**/build/**` 与其他构建产物；
- 任何临时日志或 `outputs/` 下的未归档运行结果；
- 任意 `.pem`、`.crt`、`.key`、临时证书、私钥、Bearer、usersig；
- 历史 `smoke-termination-chain` 失败日志。

历史 smoke 日志存在“项目根目录未发现、worktree 曾报告发现”的路径差异。冻结阶段必须先定位实际路径、哈希与来源；确认前统一标注为 `legacy/untrusted`，不得作为候选成功证据。

## 建议的受控执行顺序（待确认后）

1. 保存每一个候选路径的 `git diff` 与未跟踪文件内容审查记录；
2. 明确 B/C 的范围；
3. 逐路径暂存，不使用广泛通配命令；
4. 复核暂存区文件清单、diff、测试完整性、敏感文件；
5. 创建单一候选提交并记录 SHA；
6. 确认工作树干净后，按独立 QA 的归档规范重跑所有测试；
7. 只有固定 SHA 的日志、manifest 与独立 QA verdict 齐全，G6 才能从“不可核验”进入“本地集成可核验”；真机门禁仍须单独完成。

## 当前决策请求

请确认候选范围：

- **范围 A（推荐）**：运行时代码 + 迁移 + 测试 + Android/sidecar 消费者 + smoke 工具 + 真机验收文档；契约草案单独保留，待定版后再提交。
- **范围 B**：仅运行时代码 + 迁移 + 测试 + 消费者；工具与文档另开证据提交。
- **范围 C**：先仅完成逐文件审查，不创建候选提交。

无论选择哪一项，商业发布仍维持 **NO-GO**。
