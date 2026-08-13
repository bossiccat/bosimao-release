# ADR-021: 隐私四类开关的生效点映射与 MVP 边界

## Status: Accepted (2026-08-13)

## Background

SPEC AC-17（P0）要求：用户关闭云端、麦克风、后台对话或桌面捕获任一开关，系统必须立即停止对应动作；失败必须回滚 UI 设置（`docs/commercial-upgrade-SPEC.md:275`）。

当前数据层已具备（不重复造轮子）：

- `backend/app/voice/privacy.py`：四类开关定义 `cloud_processing_enabled` / `microphone_enabled` / `background_conversation_enabled` / `desktop_capture_enabled`，`PrivacyService.get/set` 已实现「写 SQLite → runtime action → 失败回滚」编排，但 `RuntimeActions` 只有 `FakeRuntimeActions`（测试用）。
- `backend/app/voice/storage.py`：`privacy_audit_events` 表已存在，`VoiceStore.write_audit()` 可用。
- `backend/app/voice/transcripts.py`：`transcript_persistence_enabled` 已实现（AC-16，默认不持久化）。

缺失（阶段 B 范围）：privacy API 端点、真实 RuntimeActions、前端 UI。本 ADR 只裁决「生效点」——开关状态如何真正影响运行链路，以及哪些是后端单端可验证、哪些是跨端（留后续）。

关键架构事实（决定 enforcement locality）：

1. 商业主媒体链路固定为 `Android mic → TRTC 云中继 → sidecar → rtc_bridge → MiniCPM-o/APM`（ADR-013）。TRTC 签发是整条云链路的唯一入口门。
2. 麦克风采集 owner 在 Android（`MicRecorder.kt`）；后台对话/锁屏 pause 也在 Android 前台服务。后端无法直接停止 Android 硬件采集。
3. 桌面捕获（WGC）完全在 Windows 后端进程内（`Orchestrator → SessionManager → WgcCapturer`），后端单端可控。

## Decision

### D1. 生效点映射（四类开关 → 精确文件/函数）

| 开关 | 生效端 | 精确生效点（file::function） | 关时动作 | 开时动作 | scope |
|---|---|---|---|---|---|
| `cloud_processing_enabled` | 后端（单端） | `backend/app/api/routes_voice_secured.py::voice_session`（L96）与 `::voice_session_sign`（L154），在调用 `deps.service.issue/sign` 之前读 `privacy.get("cloud_processing_enabled")` | 拒绝签发，返回 `40301 privacy_disabled`；读失败也拒绝（fail-closed） | 正常签发 | **in-scope** |
| `desktop_capture_enabled` | 后端（单端） | 经真实 `PrivacyRuntimeActions.apply` → `backend/app/core/orchestrator.py::Orchestrator.set_desktop_capture(enabled)`（新增方法），内部调 `stop_monitoring()` + `self._sessions.stop_all()` | 停止监控循环 + 停止全部 WGC 会话 + 释放帧文件 | `start_monitoring()` + `locate_all()` + 对已授权窗口 `start_wgc()` | **in-scope** |
| `microphone_enabled` | 跨端（采集在 Android） | 后端：`backend/app/voice/privacy.py` 状态存储 + `GET/PATCH /api/v1/privacy` + 审计（in-scope）；Android：`mobile-app/app/src/main/java/com/jax/voice/voice/VoiceForegroundService.kt::startPipelineInner`（L95）与 `::restartMicRecorder`（L261）在启动 `MicRecorder` 前拉取 `/api/v1/privacy` 判定；最终硬件采集点 `MicRecorder.kt::start`（L56） | 后端存状态+下发；Android 不启动/停止采集 | 恢复采集 | 状态+端点+审计 **in-scope**；客户端实时停采集 **out-of-scope**（阶段 B+/Android 迭代） |
| `background_conversation_enabled` | 跨端（FGS+唤醒监听在 Android） | 后端：状态 + 端点 + 审计（in-scope）；Android：`VoiceForegroundService.kt::startPipelineInner` 的 `wakeActive = VoiceConfig.wakeEnabled(this)`（L114）决定是否加载 `WakeWordEngine`；后台/锁屏 pause+flush+退房在 `VoiceSessionCoordinator` 与前台服务生命周期 | 不后台常驻监听、锁屏 pause+退房 | 恢复后台对话 | 状态+端点+审计 **in-scope**；客户端生效 **out-of-scope** |

「关时/开时动作」语义以 `docs/commercial-upgrade-ARCHITECTURE.md §4.8`（L156–159）为权威来源，逐字对齐。

### D2. API 契约（阶段 B 新增）

统一响应 `{"code":0,"data":{},"message":""}`，版本前缀 `/api/v1/`。

**GET `/api/v1/privacy`** —— 读全部开关（owner / device / sidecar 任一有效主体，复用 `routes_voice_secured._resolve_status_principal` 模式）。

```json
{
  "code": 0,
  "data": {
    "settings": {
      "cloud_processing_enabled": true,
      "microphone_enabled": true,
      "background_conversation_enabled": true,
      "desktop_capture_enabled": true,
      "transcript_persistence_enabled": false
    }
  },
  "message": ""
}
```

**PATCH `/api/v1/privacy/{setting}`** —— 设单开关（owner only + `X-Request-Nonce` + 限流，复用 `owner_or_error` 模式）。请求体：

```json
{ "enabled": false }
```

响应（对齐 `PrivacyService.set` 返回）：

```json
{
  "code": 0,
  "data": {
    "setting": "cloud_processing_enabled",
    "applied_at": 1753000000.0,
    "effective_value": false,
    "action_result": "ok"
  },
  "message": ""
}
```

`setting` 路径取值：`cloud_processing` / `microphone` / `background_conversation` / `desktop_capture` / `transcript_persistence`，映射到 `*_enabled` 键。

**新增错误码**（`backend/app/voice/errors.py` 追加，仅追加不改旧值）：

| code | HTTP | message | 用途 |
|---|---|---|---|
| `40301` | 403 | `privacy_disabled` | `cloud_processing_enabled=false` 时签发被拒 |
| `50302` | 503 | `privacy_action_failed` | 运行时动作 apply 失败、设置已回滚，可安全重试 |

失败回滚语义：`action_result="failed"` 时 `effective_value` 返回旧值（`privacy.py::PrivacyService.set` 已实现回滚），路由层同时返回 `50302 privacy_action_failed`。

### D3. 审计（每次切换强制记录）

`PrivacyService.set()` 内增加审计写入，actor 由路由层透传（owner principal.subject_id，`/api/v1/privacy` 无 owner 上下文时记 `"local"`）：

```text
store.write_audit(
    action="privacy.toggle", subject_type="setting", subject_id=setting,
    result="ok" | "failed",
    metadata_redacted_json={"old": previous, "new": enabled, "actor": actor},
)
```

`old/new` 为纯布尔，落入 `privacy_audit_events.metadata_redacted_json`，不含任何敏感明文。回滚路径也写一条 `result="failed"`。

### D4. RuntimeActions 真实化

新增 `PrivacyRuntimeActions(RuntimeActions)`（`backend/app/voice/privacy.py` 或同层新模块），替换装配处的 `FakeRuntimeActions`：

- `apply("desktop_capture_enabled", enabled)` → 经 late-bound 引用调 `Orchestrator.set_desktop_capture(enabled)`；失败抛异常 → `PrivacyService.set` 自动回滚。
- `apply("cloud_processing_enabled", ...)` → no-op（强制靠读时门禁 D1，无进程内状态副作用）。
- `apply("microphone_enabled"|"background_conversation_enabled", ...)` → no-op（后端无状态副作用，客户端生效）。
- `rollback(...)` → 逆操作（desktop_capture 恢复；其余 no-op）。

late-bound 装配约束：`Orchestrator` 在 `main.py::lifespan` 内创建，而 `PrivacyService` 在 `_build_secured_session_router`（模块装配期）创建，二者不同生命周期。采用与 `routes_capture.orchestrator` 相同的模块级 holder 间接引用，`lifespan` 里 `privacy_runtime.bind(orchestrator)` 完成绑定；`desktop_capture_enabled` 在 bind 前 apply 时记 `action_result="failed"`（不静默成功）。

### D5. MVP 边界（in-scope / out-of-scope）

**in-scope（阶段 B 必做，可验证）**

1. `GET /api/v1/privacy` + `PATCH /api/v1/privacy/{setting}` 四类开关 + 转写持久化（owner/device/sidecar 读，owner 写）。
2. `cloud_processing_enabled` 读时门禁：false 时 `voice_session`/`voice_session_sign` 返回 40301，可 curl 验证不签发 userSig。
3. `desktop_capture_enabled` 即时停/启：false 时 `SessionManager.stop_all()` + 停止监控循环，`capture_status` 变为 none/status-only、无新帧，可验证。
4. 每次切换审计（谁/什么开关/旧值→新值），落 `privacy_audit_events`。
5. `FakeRuntimeActions` 保留供测试，生产装配切换为真实 `PrivacyRuntimeActions`。

**out-of-scope（状态已存，生效点跨端，留后续迭代）**

1. 已签发 userSig 的实时撤销/踢房（需 TRTC termination，`rtc_termination_enabled` 当前 `False`，ADR-014 已预留但未实现）。关 `cloud_processing_enabled` 后存量会话自然过期（≤600s），MVP 接受。
2. `microphone_enabled` 的 Android 实时停采集（需客户端轮询 `/api/v1/privacy` 或推送通道 + `MicRecorder`/`VoiceForegroundService` 改造）。
3. `background_conversation_enabled` 的 Android 后台/锁屏 pause+flush+退房（客户端改造）。
4. `path=apm` 的 `ApmBridge` 云端推理即时中断（legacy 非生产路径，生产只走 TRTC 签发门）。

## Consequences

正面后果：

- 两条后端单端可验证的开关（cloud / desktop）闭环，AC-17 有可验收证据；隐私开关不再只是 UI 假开关。
- 明确跨端边界，阶段 B 不膨胀为 Android 改造；mic / background 的客户端生效契约已留档到文件/函数级，后续迭代可照图施工。
- 审计强制记录 old→new，满足「谁、什么开关、旧值、新值」合规要求。

负面后果：

- `microphone_enabled` / `background_conversation_enabled` 在阶段 B 只能「存状态 + 下发」，用户关掉后 Android 端不会立即停采集，需在 UI 明示「该开关需手机端配合生效」（避免 PRD §79「关闭即时停止」在手机端未闭环的信任风险）。这是阶段 B 的已知边界，不是缺陷。
- 新增 2 个错误码（40301 / 50302）需同步 errors.py 与 OpenAPI。

## Alternatives

- **四类开关全部后端单端实现**：不可行——麦克风/后台对话的采集 owner 在 Android，后端无法停止手机硬件采集，强行在后端「模拟生效」会造成 UI 假开关（违反 ADR-018「关闭只更新 UI 拒绝」）。
- **阶段 B 直接做 Android 跨端轮询 + 推送**：拒绝——超出阶段 B 工期，违反「信任优先、每阶段有验收证据」原则，应拆为独立迭代。
- **cloud_processing 用「签发后踢房」实时生效**：拒绝进入 MVP——依赖 TRTC termination（`rtc_termination_enabled` 未实现），签发门禁已满足 fail-closed，存量会话 600s 内自然过期可接受。

## Related ADRs

ADR-012（TRTC 传输）、ADR-013（商业 RTC 主路径）、ADR-014（fail-closed）、ADR-018（本地最小隐私数据）、ADR-020（TLS 四端）。
