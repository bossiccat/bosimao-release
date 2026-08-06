# 推送 Provider 组件契约（A9）

> 版本：v1.0（M-1 修复基线）
> 日期：2026-08-03
> 状态：已确认 · 后端照做（现状代码已对齐本契约，本文件为验收真源）
> 依据：docs/decisions/ADR-005-push-provider.md、docs/openapi.yaml（PushResult / test-push）、backend/app/push/base.py、backend/app/push/manager.py、backend/app/push/wecom.py、backend/app/push/ntfy.py、backend/tests/unit/test_push_manager.py

---

## 1. 接口契约（PushService / PushResult — 与 base.py 严格一致）

```python
# backend/app/push/base.py（契约真源，本文件不复制实现，只固化语义）
@dataclass
class PushResult:
    ok: bool
    provider: str
    error: str | None = None
    retryable: bool = False   # True=网络类错误（可重试）；False=业务错误（重试无意义）

class PushService(Protocol):
    name: str
    def push(self, text: str, image: Path | None = None, title: str | None = None) -> PushResult: ...
```

**语义约束**：
- `ok=True`：推送已送达目标网关（wecom HTTP 200 + errcode=0；ntfy HTTP 2xx）。
- `retryable=True`：仅限网络类（连接失败/超时/5xx 网关）；`False`：业务类（参数错/非 JSON/4xx 业务码）。
- 抛异常 = 未预期逃逸，由 `PushManager._call` 兜底捕获记失败；Provider 自身**不得**静默吞错。

## 2. PushManager 行为契约（与 manager.py 一致）

| # | 行为 | 现有实现 | 验收测试 |
|---|---|---|---|
| R1 | 按 `cfg.providers` 顺序路由，第一个成功即返回 | ✅ | TestRouting::test_first_success_wins |
| R2 | a 业务失败 → 路由 b | ✅ | TestRouting::test_failover_to_next |
| R3 | 网络类错误每 Provider 重试 1 次（共 2 次调用） | ✅ | TestRetry::test_network_error_retried_once |
| R4 | 业务错误不重试（仅 1 次调用） | ✅ | TestRetry::test_business_error_not_retried |
| R5 | Provider 抛异常 → 管理器捕获记为失败，不向外抛 | ✅ | TestRetry::test_exception_does_not_escape |
| C1 | 连续失败 ≥ fail_threshold(3) → 熔断；冷却期不再调用 | ✅ | TestCircuitBreaker::test_open_after_threshold |
| C2 | 冷却到期 → 半开 probe；成功 → fail_count 归零关闭 | ✅ | TestCircuitBreaker::test_half_open_probe_success_closes |
| C3 | probe 失败 → 立即重新熔断 | ✅ | TestCircuitBreaker::test_half_open_probe_fail_reopens |
| L1 | 限频由 Provider 自实现；wecom 仅成功后占配额 | ✅ | TestWecomRateLimit::test_quota_only_on_success |
| L2 | 非 JSON 响应不逃逸、判业务失败 | ✅ | TestWecomRateLimit::test_non_json_response_no_escape |

**熔断参数**（config/push.yaml → `PushConfig.circuit_breaker`）：`fail_threshold=3`、`cooldown_seconds=300`；半开期间仅放行 1 个 probe。

## 3. Provider 验收

### 3.1 WecomProvider
- 构造：`WecomProvider(webhook_url, rate_limit_per_minute)`；`enabled=False`/缺 webhook → 构造抛 `ValueError`，`PushManager` 捕获后跳过（记 warning）。
- push：POST webhook，JSON body `{"msgtype":"text","text":{"content":...,"mentioned_mobile_list":[]}}`；响应 JSON `{"errcode":0}` 视为成功；`errcode!=0` → 业务失败（`retryable=False`）；非 JSON → 业务失败不逃逸。
- 限频：`rate_limit_per_minute` 滑动窗口；**仅成功占用配额**；满配额时 `_allow()=False` → 返回失败（`retryable=True`，给管理器换下一 Provider 的机会）。
- image：V1 wecom 文本通道不携带截图（O-006 用户裁决：脱敏文本、不含截图）。

### 3.2 NtfyProvider
- 构造：`NtfyProvider(server, topic, priority)`；缺 topic → `ValueError`。
- push：POST `{server}/{topic}`，header `Title`/`Priority`，body = text；2xx 成功；5xx/超时 → 网络类（`retryable=True`）；4xx → 业务失败。
- `with_screenshot=True`（config）：POST multipart 附 image；失败仅降级为纯文本推送，不吞异常。

## 4. 测试要求（契约即测试基线）

- 全部单测与 `tests/unit/test_push_manager.py` 现有用例一致且持续绿灯；新增 Provider 必须新增同风格用例（FakeProvider/BoomProvider 模式）。
- 覆盖维度：路由顺序、熔断开关、半开恢复/重断、重试次数、异常不逃逸、限频计数、非 JSON 不逃逸（§2 表全项）。
- 命令：`cd backend && pytest tests/unit/test_push_manager.py -q`（新增 provider 用例并入同文件或 `test_<provider>.py`）。

## 5. 归属判断

| 项 | 归属 |
|---|---|
| PushManager 熔断/重试/路由/限频框架 | **V1 立即实现**（现状代码已达标，仅验收） |
| wecom 截图通道、ntfy multipart 图 | **V1.1**（O-006 裁决先脱敏文本；截图通道待隐私边界定稿） |

## 6. E2E 验收

1. `POST /api/v1/control/test-push` → 手机/终端实际收到文本推送，返回 `{"ok":true,"provider":"wecom"|"ntfy"}`（openapi PushResult）。
2. 断网/错误 webhook 连续 3 次 → 日志熔断 warning；冷却期再次 test-push 不调用该 provider，返回失败但进程不崩。
3. 恢复网络冷却到期 → 半开成功 → 熔断关闭，推送恢复。
