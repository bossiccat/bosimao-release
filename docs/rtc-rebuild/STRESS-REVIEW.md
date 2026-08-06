# 高压测试独立复核报告（STRESS-REVIEW）

> 复核人：QA（严过关）· 日期：2026-08-06 · 复核对象：docs/rtc-rebuild/STRESS-REPORT.md
> 原则：**独立验收，不许自证；不得只信报告要自己跑；测试完整性反作弊门必过**

## Verdict: ✅ PASS

STRESS-REPORT.md 的结论与关键数字经独立复核**全部复现**。2 个 P1 修复（意图协调误报、顶替竞态）代码审计成立 + 行为验证通过。无新增 P0/P1 缺陷。

---

## 一、独立重跑结果（与报告数字对比）

### 1. 单测（独立重跑）

| 套件 | 命令 | 报告 | 独立重跑 | 一致 |
|---|---|---|---|---|
| rtc_bridge server | `pytest tests/unit/test_rtc_bridge_server.py -q` | 7 passed | **7 passed** | ✅ |
| backend 全量 | `pytest tests/unit -q` | 342 passed | **342 passed** | ✅ |
| 云函数 Node | `node --test "test/*.test.js"` | 10 pass | **10 pass**（5 signing + 5 usersig） | ✅ |

> ⚠️ 环境注记：backend 全量首跑出现 1 个 `F` 且 exit=1，根因是**沙箱批量删除拦截**——pytest 默认在系统 Temp 清理 `pytest-of-Administrator/garbage-*`（798 文件）被 sandbox 拒绝，导致 teardown 异常退出。改用隔离 `--basetemp` 后 **342 passed, exit=0**。属环境问题，非代码缺陷。

### 2. 关键压测场景（真公网 / 本地 WS，独立重跑）

| 场景 | 报告 | 独立重跑 | 一致 |
|---|---|---|---|
| S3 意图风暴 12 路 | pending 12/12 + 消费 12/12 | **pending 12/12 + 消费 12/12，目标残留 0** | ✅ |
| S7 高频音频流 | up_frames 增量 300 | **up_frames 增量 300（300/300 帧）** | ✅ |
| S6 顶替语义（补充验证） | A closed / B ready | **A `ConnectionClosedOK` / B ready** | ✅ |

> ⚠️ 环境注记（重要，非代码缺陷）：**压测遗留的 PC sidecar（Electron，pid 16060，`--hold=86400`）仍持有 bridge 单槽**，且其设计行为就是"轮询 pending → 消费意图 → 进房"。
> - S3 首跑 FAIL（pending 10/12、消费 9/12）：正是该 sidecar 抢消费了部分意图（反向证明意图链路真实工作，与报告§四一致）。
> - S7 首跑 FAIL：测试连接被 sidecar 立即顶替（`received 1000 replaced`）。
> - 释放 bridge（临时停 sidecar，跑完已还原）后：S3/S6/S7 全部 PASS，数字与报告一致。
>
> **结论**：报告数字可复现；首跑 FAIL 是测试环境残留 sidecar 竞争所致，不是修复无效。

### 3. 验签抽查（独立）

- 真实云函数签发 `POST /api/v1/voice/session {"device_id":"qa-verify-1"}` → 取得 user_sig。
- 用 `deploy/trtc-sign/test/verify.js`（官方 TLSSigAPIv2 独立反解，不 import genUserSig 自证）+ 真实 TRTC_SECRETKEY（**仅经环境变量注入内存，未写入任何文件/报告/输出**）验签：
  - `sigValid=true`、`appIdMatch=true`、`expireOk=true`（expire=600，满足契约 ≤600）、`identifier=qa-verify-1`、`ver=2.0` → **PASS**

### 4. 消费语义直验（补充）

手机唤醒 → PC 首次消费 `code:0 + userSig` → 二次消费 **`40401` 拒绝**。条件更新防重复消费语义真实生效。

---

## 二、代码审计

### P1-1 意图协调误报（signing.js consume()）

**审计项**：`if (!up || Number(up.updated) !== 1) return null;`

- 已核对 `node_modules/@cloudbase/node-sdk/types/db.d.ts`：
  ```
  export interface IUpdateResult extends IBaseResult {
    updated?: number
    upserted?: JsonString
  }
  ```
  → `updated` 是 **IUpdateResult 顶层字段**，非 `stats.updated`。v1.1 曾误读 `up.stats.updated` 确为 bug；现读 `up.updated` 正确。
- **条件更新防重复消费语义成立**：`where({ _id, consumed: false }).update({ consumed: true, ... })` 为数据库原子条件更新——首次匹配 1 条 → `updated=1` 成功；已消费后再 update 匹配 0 条 → `updated=0` → null → 40401。直验（见§一.4）证明语义真实生效。
- **多实例共享成立**：意图存 NoSQL `voice_intents`（`_id=device_id`），SCF 多实例共享 DB，listPending 全局可见（S3 12/12 复现）。v1.0 内存 dict 缺陷确被消除。

**结论**：修复正确，无自证（mock 测试用 `{ updated }` 模拟 SDK 真实返回，与类型定义一致）。

### P1-2 顶替竞态（backend/rtc_bridge/server.py）

**审计项**：先接管再 close 的顺序 + `_cleanup(ws)` 身份检查 + 旧 session 显式释放。

```
old = self._ws
old_session = self._session
self._ws = ws            # ① 先接管（关键：在 await old.close() 之前）
if old is not None and old is not ws:
    await old.close(...)
if old_session is not None:
    await old_session.close()
    self._session = None
...
finally:
    await self._cleanup(ws)
```

```python
async def _cleanup(self, ws) -> None:
    if self._ws is not ws:   # ② 身份检查
        return
    ...
```

- **竞态窗口已消除**：旧 handler 的 `finally → _cleanup(old_ws)` 执行时，`self._ws` 已是新 ws → `self._ws is not ws` 为真 → 提前 return，**不再误清新连接**。修复前 `self._ws` 在 `await old.close()` 期间仍指向 A，身份检查通过 → 误伤 B——根因分析成立。
- **身份检查完整性**：`_cleanup(ws)` 以参数携带连接身份，与 `self._ws` 比对；新连接自己的 finally 清理不受影响（`self._ws is ws` 为真 → 正常清理）。顶替场景与正常断开场景均正确。
- **无泄漏**：旧 session 由新连接 handler 显式 `old_session.close()` 并置 `self._session=None` 兜底（旧 handler 清理被身份检查跳过，若不做此步会泄漏 APM 会话）。审计 `session.close()`：仅关闭 APM/shaper/取消 consumer，**不向 `_send_msg` 发任何消息** → 旧 session 释放不会把消息写到新连接，无跨连接串扰。
- **回归测试有效**：`test_replace_semantics_old_cleanup_does_not_kill_new` 覆盖"A 被顶 → A 的 finally 清理必须跳过 → B 的 session 存活且可收流"（断言 `sidecar_connected=True` + `bridge._session.device_id=="dev-b"` + up_audio 进入 B 的 fake_apm）。7/7 通过，断言非硬编码实现输出。

**结论**：修复正确，顺序正确（先接管 → 再 close → 显式释放旧 session → 身份检查清理），竞态窗口关闭，无泄漏。

---

## 三、测试完整性反作弊门（P0 级门禁，基线 f6e9348）

| 检测项 | 结果 | 证据 |
|---|---|---|
| 测试文件删除 | ✅ 无删除 | 后端测试文件 42 → 45（+3，含新增顶替回归） |
| 断言数下降 | ✅ 无弱化 | 后端 assert 总数 810 → 858（**+48**） |
| skip/xfail/.only 新增 | ✅ 无 | git diff 无新增 skip/xfail/.only/focus |
| 硬编码断言 | ✅ 无 | 回归测试断言来自 Spec 语义（B 存活 + 收流），非实现返回值复述 |
| 框架配置篡改 | ✅ 无 | pyproject.toml / package.json / pytest 配置零变更 |

**门禁结论：通过**，无 AI 作弊痕迹。

---

## 四、视觉合规抽查（团队 P0/P1 规则）

| 规则 | 结果 | 证据 |
|---|---|---|
| emoji 作为功能图标（P0） | ✅ 合规 | 仅二进制资源（launcher/icon PNG）与代码注释含 emoji；无 UI 功能图标用 emoji |
| 紫色→粉色渐变（P1） | ✅ 合规 | pet-ui/src / mobile-app 零匹配 |
| AI 模板味文案（P1） | ✅ 合规 | Welcome to / Lorem ipsum / Sign up today 零匹配 |

---

## 五、发现的偏差 / 待办（非阻断）

1. **报告与保存 JSON 的延迟口径不一致（文档级，P2）**：报告 S1 写"P50 499ms / P95 580ms（热实例）"，但 `tmp/stress_cloud_results.json` 存的是含冷启动的整批延迟（P50 3058ms / P95 3543ms）。报告正文已注明"冷启动 ~3s"，但建议在报告里标注两套数字口径，避免读者混淆。
2. **压测环境清理待补（流程级，P2）**：压测遗留 sidecar（`--hold=86400`）与 rtc_bridge 单槽设计叠加，会干扰后续压测（本次 S3/S7 首跑即被其竞争）。建议压测脚本结束主动释放 sidecar 或压测专用 device 前缀 + 事后清理（报告§四已清理 voice_intents，但 sidecar 进程本身未停）。
3. **S6 报告字段 `sidecar_connected:false` 易误读（文档级）**：该值是压测脚本 B 连接退出后采样（脚本上下文管理器退出即断开），非顶替失败。建议脚本在 B 存活期间采样，或报告注明口径。
4. **遗留待办与报告§六一致**：看门狗防双实例（P2）、累计指标（P2）、冷启动 keep-warm（P2）、git commit（流程）——均非本次复核阻断项。

---

## 六、结论

- **verdict: PASS**。STRESS-REPORT.md 所述 2 个 P1 修复（意图协调误报、顶替竞态）经代码审计 + 独立重跑 + 行为直验全部成立；10/10 场景中的关键场景（S3/S6/S7）独立复现；单测 7+342+10 全绿；真实 userSig 独立验签通过；测试完整性反作弊门通过。
- 未发现新增 P0/P1 缺陷。3 条非阻断建议（延迟口径、压测环境清理、S6 字段口径）建议入库待办。
- 生产就绪：本复核范围（高压测试 + 修复 + 回归）达 **Silver** 档。**真机跨网验收（10 步清单）仍为最终裁判，未完成前不建议商业生产放行。**

### 证据索引
- 单测日志：本次会话独立执行（7 passed / 342 passed / 10 pass）
- 压测日志：S3 `pending 12/12 + 消费 12/12`、S6 `A closed / B ready`、S7 `up_frames 增量 300`
- 验签输出：`sigValid=true appIdMatch=true expireOk=true identifier=qa-verify-1 expire=600`
- 反作弊门：assert 810→858，测试文件 42→45，零删除/零 skip/零配置篡改
