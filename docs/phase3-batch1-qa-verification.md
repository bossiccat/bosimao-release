# Phase 3 Batch 1 QA 独立复核验证报告（最终版）

> 角色：mvp-dev-expert-team-qa（fresh-eyes checker，不采信后端自评）  
> 日期：2026-08-07（v1：Task 1-3 复核；v2：Task 4 复跑；v3：依赖机械证据闭环，最终裁决；v4：Task 5 追加复核）  
> 复核对象：Task 1-5 后端落盘产物（四文件验收测试 + storage/auth/nonce/rate_limit/errors/repositories + 生产 fail-closed + 测试迁移 + 旧网关下线 + 有界音频队列）  
> 验证环境：`C:/Users/Administrator/WorkBuddy/监视app/.venv/Scripts/python.exe`（Python 3.11.9 / pytest 9.1.1）  
> 复核方式：只读验证，未修改任何生产代码与测试文件

---

## 1. RoleVerdict

```yaml
RoleVerdict:
  verdict: pass
  scope: Phase 3 Batch 1（Task 1-4）独立复核
  resolved:
    - id: QA-VER-001  # 旧匿名网关 fail-open P0
      rule: SPEC v1.1 §5/§9.1 / ADR-014
      evidence: main.py lifespan 已改为 `if not app_config.settings.voice_production` 才挂载
               build_voice_gateway；生产下 /ws/voice、旧 /stream、旧 /status、/pair 均不可达。
      expected: 已满足；建议补 main.py 装配层测试锁定条件挂载（advisory）。
    - id: QA-VER-002  # 精确依赖与警告治理
      rule: SPEC §4/§12.3
      evidence: requirements.txt 全部 "=="（fastapi==0.141.1 ... pytest-asyncio==1.4.0），
               pet-ui/package.json 全部精确无 ^/~；QA 独立复核：
               pip check -> "No broken requirements found" exit 0；
               pip install --dry-run -r requirements.txt -> exit 0（全部满足）；
               npm ci --dry-run（pet-ui）-> exit 0，added 60 packages；
               契约测试 5/5 passed（精确依赖断言含 lucide-react=0.469.0 唯一图标源）。
               StarletteDeprecationWarning 确认源自 fastapi 0.141.1 内部导入
               starlette.testclient 的上游迁移期行为，非本项目代码；已登记 O-017
               （不 suppress、推荐 Task 14 干净构建复查、上游稳定后升级），处理合理。
      expected: 已满足。
  advisory:
    - test_voice_security_routes.py 的 stream 升级用例使用 pytest.raises(Exception) 宽泛断言，
      建议后续按 WS 关闭码（4401/4402/4403/4503）精确断言。
    - 建议补 main.py 装配层测试，锁定 voice_production 条件挂载行为。
    - VoiceStore 连接工厂用 lambda（E731 豁免），可读性建议改善，非阻断。
    - Python 依赖以 requirements.txt 精确锁定 + pip 可满足性证据为准；如需提交级
      可复现锁文件，可评估 pip-tools/uv 生成 requirements.lock（非本批阻断项）。
```

**Batch 1（Task 1-4）P0 缺陷：0。QA RoleVerdict：pass（针对 Batch 1 范围）。**

---

## 10. Task 5 追加复核（v4，2026-08-07）：有界音频与背压（AC-08/09/10）

### 10.1 测试复跑

命令：
```bash
./.venv/Scripts/python.exe -u -m pytest backend/tests/unit/test_audio_frame_buffer.py \
  backend/tests/unit/test_bounded_audio_queue.py backend/tests/unit/test_rtc_bridge_server.py \
  -q -ra --tb=short -p no:cacheprovider
```
结果：**24 passed, 0 failed/skipped/xfailed**（2.94s）。用例分布与后端自评吻合：frame_buffer 7 + bounded_queue 8 + rtc_bridge_server 9（含 3 个 Task 5 shaper 集成：`test_shaper_outputs_only_full_640_frames`、`test_shaper_backpressure_drops_oldest` + 既有 7 个桥接/生命周期用例）。

### 10.2 实现抽读（真实实现，非 stub）

| 文件 | 行为核对 | 判定 |
|---|---|---|
| frame_buffer.py（57 行） | 跨块保留 residue 只输出完整 640B；tail_mode=drop/pad 显式处理尾帧并记录 `tail_dropped_bytes/tail_padded_frames`；reset 清残留防串话 | ✅ 真实实现 |
| bounded_audio_queue.py（123 行） | 条目携带 generation/created_at/size；入队同时检查 max_frames/max_bytes/max_frame_age_ms；过载丢旧保新（popleft）；`flush(generation)` 旧代丢弃、`bump_generation` 打断语义；记录 depth/high_watermark/drops/backpressure_events | ✅ 真实实现 |
| shaper.py | DownlinkShaper = PcmFrameBuffer + BoundedAudioQueue + 20ms 节拍推送；`push()` 仅拆帧+有界入队+Event 唤醒（非阻塞）；尾帧 flush 显式处理 | ✅ 真实实现 |
| session.py | 上行 `_up_q` 同样 BoundedAudioQueue（100 帧预算）+ 独立消费协程 feed APM；`on_up_audio`/`_on_audio_out` 仅入队/唤醒（非阻塞）；peer enter 时 shaper.reset 防跨会话污染 | ✅ 真实实现 |

### 10.3 无界队列替换核对

- `grep asyncio.Queue backend/rtc_bridge/*.py`：**0 命中**。shaper/session 全部使用 `BoundedAudioQueue`（collections.deque 实现），无遗留无界 asyncio.Queue。
- 音频回调非阻塞：上行 `on_up_audio` 同步 push + set Event；下行 `_on_audio_out` 仅 `await shaper.push()`（拆帧 + 有界入队，无网络 IO 在回调内）；网络发送在独立 `_run`/`_consume_up` 协程中。

### 10.4 health /metrics 队列指标

- health.py `_metrics()` 在 `_session_ref` 存在时输出：`up_queue_depth / down_queue_depth / queue_high_watermark / queue_drops / backpressure_events` + `up_frames / down_frames / apm_session_state / last_peer_ts`；并弹出 `_session_ref` 防不可序列化泄漏。
- `test_health_metrics_serializable` 验证连接中 metrics 可 json 序列化且不含 `_session_ref`。

### 10.5 全量回归

命令：
```bash
./.venv/Scripts/python.exe -u -m pytest backend/tests -q -ra --tb=short -p no:cacheprovider
```
结果：**438 passed, 0 failed/skipped/xfailed**（42.45s）。Task 4 基线 421 → 438（+17：frame_buffer 7 + bounded_queue 8 + shaper 集成 2），无回归。

### 10.6 Task 5 RoleVerdict

```yaml
RoleVerdict_Task5:
  verdict: pass
  scope: Phase 3 Task 5（有界音频与背压）
  blocking: []
  advisory:
    - BoundedAudioQueue._drop_expired 仅在 push/pop 时惰性清理；若消费停滞且无新 push，
      队列可能长时间持有超龄条目（内存不增长但帧龄语义依赖下次 push/pop 触发）。
      建议 Task 13 指标/故障注入时验证该场景或加定时清理。
    - shaper._run 异常路径仅记 warning 继续循环，无退避；sidecar 长期断线时 20ms 空转，
      建议后续评估退避（非阻断，Task 13 覆盖）。
  evidence:
    - command: pytest 三个 Task 5 测试文件 -q -ra
      result: 24 passed, 0 skip
    - command: pytest backend/tests -q -ra
      result: 438 passed, 0 skip（421 → 438，无回归）
    - code review: frame_buffer/bounded_audio_queue/shaper/session/health 逐文件核对
```

---

## 2. 复核执行证据（命令 + 退出码）

### 2.1 四文件门禁

命令：
```bash
./.venv/Scripts/python.exe -u -m pytest backend/tests/contract/test_commercial_contract.py \
  backend/tests/unit/test_voice_storage.py backend/tests/unit/test_voice_auth.py \
  backend/tests/integration/test_voice_security_routes.py -q -ra --tb=short -p no:cacheprovider
```
结果：**56 passed, 1 warning, 0 failed, 0 skipped, 0 xfailed**（43.31s）。与后端自评 "56/403" 的前 56 吻合。

### 2.2 backend/tests 全量

命令：
```bash
./.venv/Scripts/python.exe -u -m pytest backend/tests -q -ra --tb=short -p no:cacheprovider
```
结果：**421 passed, 1 warning, 0 failed, 0 skipped, 0 xfailed**（109.58s，0:01:49）。后端自评 403，QA 实测 421（含 QA 审计报告要求之外的既有用例差异，无失败、无跳过）。

唯一警告：`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead`（FastAPI TestClient）。

---

## 3. 测试迁移：显式迁移，非删弱（对照 git diff HEAD~1）

| 文件 | 变化 | 判定 |
|---|---|---|
| test_rtc_session_sign.py | HTTP 200→201；scene `audio_call`→`trtc_full_duplex`；新增 `session_id`、`expires_at` 断言 | ✅ 显式迁移（+9 行，0 删断言） |
| test_voice_session.py | HTTP 200→201；scene 迁移；新增 `session_id`、`expires_at` 断言（issue 与 route 两层） | ✅ 显式迁移（+11 行，0 删断言） |
| test_voice_session_qa.py | HTTP 200→201；scene 迁移；新增 `session_id`、`expires_at` 断言；docstring 同步说明 | ✅ 显式迁移（+8 行，0 删断言） |
| test_rtc_bridge_server.py | 新增 52 行（+52） | ✅ 新增 |

**断言统计（全 backend/tests）**：
- HEAD~1：45 个测试文件，859 个 `assert/expect` token
- 复核时：51 个测试文件，1114 个 `assert/expect` token（净增 +255，无删除、无弱化）
- 三个迁移文件均无断言删除；迁移方向与 OpenAPI（201 / trtc_full_duplex / session_id / expires_at）一致

**结论：迁移是契约升级的显式更新，不是删弱断言换绿。**

---

## 4. 反作弊扫描（skip/xfail/.only/focus）

- 四文件门禁与全量均 0 skipped / 0 xfailed。
- 新增测试文件（contract/storage/auth/integration 四文件）skip 类标记计数均为 0。
- 全 diff 新增行扫描命中仅 `@pytest.mark.asyncio`（test_rtc_bridge_server.py，属异步测试标记，非 skip；`skip` 为 "asyncio" 子串误报）。
- 无 `pytest.importorskip`、`.only`、`focus`、条件 return 吞失败。

---

## 5. 生产 fail-closed 实测（独立脚本，非测试套件内）

| # | 场景 | 实测结果 | 判定 |
|---|---|---|---|
| 1 | 匿名 `POST /api/v1/voice/session`（无 Bearer/nonce） | HTTP 401, code=40101, msg=auth_failed | ✅ |
| 2 | `production=True` 且 `TRTC_SECRETKEY` 空 | `ProductionGateError: 生产安全能力缺失: trtc_secret_key`（拒绝启动） | ✅ |
| 3 | `production=True` 且 TLS 关闭 | `ProductionGateError: 生产安全能力缺失: tls_enabled`（拒绝启动） | ✅ |
| 4 | `production=False` 缺 TRTC secret（开发态） | 可启动，但 `/session` 返回 HTTP 503, code=50300（运行时关闭，不 fail-open） | ✅ |
| 5 | 生产完整配置 + 正确凭证 | HTTP 201, code=0（正常签发） | ✅ |

**fail-closed 生效：生产缺 TLS/validator/rate-limit/TRTC secret 拒绝启动；开发态缺凭据端点运行时 50300，绝不匿名放行。**

---

## 6. 分层与行数（生产单文件 ≤300 行）

| 文件 | 行数 | 判定 |
|---|---:|---|
| backend/app/voice/storage.py | 271 | ✅ ≤300 |
| backend/app/voice/auth.py | 98 | ✅ |
| backend/app/voice/nonce.py | 25 | ✅ |
| backend/app/voice/rate_limit.py | 47 | ✅ |
| backend/app/voice/errors.py | 44 | ✅ |
| backend/app/voice/config.py | 147 | ✅ |
| backend/app/api/routes_voice.py | 189 | ✅ |
| backend/app/api/routes_voice_secured.py | 263 | ✅ ≤300 |
| backend/app/voice/rtc_session.py | 143 | ✅ |
| repositories/（7 个模块） | 26–108 | ✅ 已拆层 |
| migrations/001_commercial_voice.sql | 119 | ✅ 九表 + 索引/唯一约束 |

**分层结论：storage 门面 + repositories 仓库拆分符合计划要求；无单文件超 300 行。**

---

## 7. main.py legacy 匿名网关现状（Task 4 复核：已下线）

- Task 1-3 复核时：`backend/app/main.py` lifespan 无条件挂载 `build_voice_gateway`，旧匿名端点（`/ws/voice`、`/api/v1/voice/stream`、`/api/v1/voice/status`、`/api/v1/voice/pair`）与 secured router 并存，记录为 fail-open P0（QA-VER-001）。
- Task 4 落盘后复核：lifespan 已改为 `if not app_config.settings.voice_production:` 才挂载旧网关（main.py 第 125-131 行），生产模式下旧匿名端点不可达；ADR-014 fail-closed 生效，QA-VER-001 已解决。
- 复核时未发现覆盖 main.py 装配层条件挂载行为的测试；建议后续补充（advisory）。

---

## 8. 变更留痕

- QA 复核期间创建的全部临时脚本/产物（qa_*.py / qa_*.txt / qa_tmp/）已清理；未提交任何代码。
- 本报告是唯一新增产物：`docs/phase3-batch1-qa-verification.md`。
- Task 4 落盘后全量回归复跑：`pytest backend/tests -q -ra` => **421 passed, 0 failed/skipped/xfailed**（66.85s；新增 4 个 pytest temp-dir 清理沙箱警告，非测试失败）。
- 依赖机械证据（v3 闭环，QA 独立复核）：
  - `pip check` => `No broken requirements found`，exit 0
  - `pip install --dry-run -r requirements.txt` => exit 0（声明集全部可满足）
  - `npm ci --dry-run`（pet-ui）=> exit 0，added 60 packages
  - `pytest backend/tests/contract/test_commercial_contract.py` => 5/5 passed（精确依赖断言）
  - StarletteDeprecationWarning 归因：fastapi 0.141.1 内部导入 starlette.testclient 的上游迁移期行为，非本项目测试代码；已登记 `docs/decisions/OPEN-DECISIONS.md` O-017（不 suppress；推荐 Task 14 干净构建复查，或上游稳定后升级）——该处置合理，同意。
  - 沙箱 temp-dir 清理 PytestWarning：Windows 沙箱回收站不可用（SAFE_DELETE_FAIL_CLOSED），环境基线，与代码无关。

---

## 9. 结论

- Task 1-3 的**四文件验收测试已落盘且全绿（56 passed）**；全量 421 passed、0 skip/xfail。
- 测试迁移是显式契约升级（200→201、audio_call→trtc_full_duplex、补 session_id/expires_at），断言净增 +255，无删弱。
- 生产 fail-closed 实测全部生效（缺 TLS/TRTC secret 拒绝启动；开发态缺凭据 50300；匿名 40101）。
- Task 4 旧匿名网关已下线（生产条件挂载），QA-VER-001 解决。
- 精确依赖机械证据闭环（pip check / dry-run / npm ci --dry-run / 契约测试），QA-VER-002 解决。
- Task 5 有界音频：24 passed 局部门禁 + 全量 438 passed 无回归；residue/尾帧/三维预算/丢旧保新/generation flush 均为真实实现；无遗留 asyncio.Queue；health /metrics 输出队列指标。
- **Batch 1（Task 1-5）P0 缺陷：0；RoleVerdict：pass（Batch 1 + Task 5 范围）。**
- 注意：本 verdict 只覆盖 Batch 1 机械与安全门禁，不替代后续 Android 真机连续两轮、打断 P95≤300ms、sidecar SDK 真机注入等 Phase 4 发布门禁（Task 12-14）。
- 分层与 300 行约束满足。

---

## 11. Task 6+7 追加复核（v5，2026-08-08 01:12 本地）：Android 生命周期与播放订阅

### 11.1 RoleVerdict（Task 6+7 代码审查）

```yaml
RoleVerdict_Task67:
  verdict: pass_conditional  # 代码审查全通过；Gradle 复跑 BLOCKED（前端并发写，见 11.5）
  blocking: []
  blocked_by:
    - id: QA-T67-BLOCK-001
      rule: 串行执行约束（team-lead 指令：Android 测试勿与前端并发）
      evidence: 复核期间检测到前端正在实时写入 Task 8 文件（MainActivity.kt 01:09:16、
               BargeInController.kt 01:11:59 持续更新；test 目录 7 个 kt 文件含 Task 8 新测试类），
               工作树处于 Task 8 中间态；compileDebugKotlin 报 Unresolved reference
               VoiceEntry/VoiceUiModel/ExperienceState（级联中间态）且 app/build/kotlin
               local-state 被占用（环境锁）。
      expected: 前端 Task 8 提交后由 QA 复跑 testDebugUnitTest 完成闭环。
  advisory:
    - RtcClient.kt 实测为 299 行（含 class 闭合括号），符合 Task 7 拆分约束。
    - RtcPlaybackSubscription.interruptPlayback 的 mute(true)+mute(false) 在同一调用栈内
      连续执行；SDK 侧若异步生效，理论上有极小窗口，但 AC-13 语义（本地 stop/flush 脉冲）
      与测试断言（muteCalls=[true,false]）一致，风险可接受。
```

### 11.2 代码审查：RtcPlaybackSubscription.kt（100 行）

| 要求 | 核对 | 判定 |
|---|---|---|
| 正常状态机零 mute | `onRemoteAudioStatusUpdated`：audioStatus=1→STARTED+SPEAKING，2→STOPPED+LISTENING，**只发 UI 事件，绝不调用 muteRemoteAudio(true)** | ✅ |
| 打断脉冲 + generation 失效 | `interruptPlayback`：`playbackGeneration++` → `muteRemoteAudio(userId,true)`（本地停播冲刷）→ 立即 `mute(false)` 恢复；不改变长期订阅 | ✅ |
| ensureUnmuted 仅兜底 | 只在 `onRemoteUserEnterRoom`/`onFirstAudioFrame`/`onUserAudioAvailable(available=true)` 调用；正常状态机不调用 | ✅ |

### 11.3 代码审查：RtcClient.kt（299 行）

- 文件行数：**299 行**（含 class 闭合括号），符合拆分要求。
- `onRemoteAudioEvent: (RtcPlaybackSubscription.RemoteAudioEvent) -> Unit` 构造参数已接线：`playback = RtcPlaybackSubscription(..., onUiEvent = { onRemoteAudioEvent(it) })`。
- listener 接线：`onRemoteAudioStatusUpdated` → `playback.onRemoteAudioStatusUpdated`；`interruptRemotePlayback()` → `playback.interruptPlayback`；进房 `muteAllRemoteAudio(false)` 防残留。
- 职责边界：会话核心（进/退房/重连映射/错误）+ playback（订阅/打断）+ RtcAudioFrameRms（波形）已拆分。

### 11.4 测试反作弊与用例数

- 4 个测试类扫描：**无 @Ignore / skip / .only / focus**（仅注释中反作弊声明）。
- 用例数核对（源文件 @Test 计数）：RtcClientTest=10、RtcPlaybackSubscriptionTest=4、RtcRemoteAudioStatusTest=3、VoiceSessionCoordinatorTest=10 → **合计 27，与预期 10+4+3+10=27 一致**。
- mute 断言非 mock-only：`every { engine.muteRemoteAudio(any(), any()) } answers { muteCalls.add(secondArg<Boolean>()) }` 记录实参并断言 `listOf(true,false)` / `muteCalls.none { it }`。

### 11.5 Gradle 复跑：BLOCKED（并发写，非源码判定）

命令（按 team-lead 指定环境）：
```bash
JAVA_HOME=C:/Users/Administrator/Downloads/jax-build/jdk17/jdk-17.0.20+8 \
GRADLE_USER_HOME=C:/Users/Administrator/Downloads/jax-build/gradle-home-v042c \
C:/Users/Administrator/Downloads/jax-build/gradle/gradle-8.7/bin/gradle.bat \
  -p C:/Users/Administrator/WorkBuddy/监视app/mobile-app testDebugUnitTest --offline --console=plain
```
结果：**BUILD FAILED in 53s**，`:app:compileDebugKotlin FAILED`：
- `Unresolved reference: VoiceEntry / VoiceUiModel / ExperienceState`（MainActivity/FloatingOverlay/VoiceController/VoiceForegroundService 引用 Task 8 尚未落盘完整的类型，级联中间态）
- `IOException: Unable to delete ... app/build/kotlin/compileDebugKotlin/local-state`（build-history.bin 被进程占用，环境锁）

证据（前端并发写实锤，本地时间）：
- MainActivity.kt lastWrite 01:09:16（18630B）、BargeInController.kt lastWrite 01:11:59（4074B，持续更新）
- test 目录 7 个 kt 文件（Task 8 新增 VoiceUiModelTest/BargeInControllerTest/VoiceEntryPointTest 正在写入）
- Task 6+7 产物时间戳已稳定：RtcPlaybackSubscription.kt 00:52:29、RtcClient.kt 00:57:08、测试文件 00:33-00:50

判定：**非 Task 6+7 源码缺陷**——工作树处于 Task 8 中间态且与前端并发写冲突，符合"勿与前端并发"指令下的 BLOCKED。另记录：QA 执行 `gradle --stop` 停止 2 个遗留 daemon 以解除 journal 锁（Java 8 launcher 遗留，已确认非本会话构建）。

---

## 12. Task 6-8 最终复跑闭环（v6，2026-08-08 01:37 本地）

### 12.1 独立复跑（fresh execution，非 UP-TO-DATE）

命令（team-lead 指定环境 + 强制全量重跑）：
```bash
JAVA_HOME=C:/Users/Administrator/Downloads/jax-build/jdk17/jdk-17.0.20+8 \
GRADLE_USER_HOME=C:/Users/Administrator/Downloads/jax-build/gradle-home-v042c \
C:/Users/Administrator/Downloads/jax-build/gradle/gradle-8.7/bin/gradle.bat \
  -p C:/Users/Administrator/WorkBuddy/监视app/mobile-app \
  testDebugUnitTest --rerun-tasks --offline --console=plain
```
结果：**BUILD SUCCESSFUL in 1m55s，23 actionable tasks 全部执行**（非缓存）；`compileDebugKotlin` 通过（仅 2 个 unused-parameter warning）；`testDebugUnitTest` 重新执行。QA_GRADLE_EXIT=0。

### 12.2 46 用例断言（17:37 重新生成的 XML，QA 独立读取）

| 测试类 | tests | skipped | failures | errors |
|---|---:|---:|---:|---:|
| net.RtcClientTest | 10 | 0 | 0 | 0 |
| net.RtcPlaybackSubscriptionTest | 4 | 0 | 0 | 0 |
| net.RtcRemoteAudioStatusTest | 3 | 0 | 0 | 0 |
| voice.VoiceSessionCoordinatorTest | 10 | 0 | 0 | 0 |
| voice.VoiceUiModelTest | 6 | 0 | 0 | 0 |
| voice.BargeInControllerTest | 7 | 0 | 0 | 0 |
| voice.VoiceEntryPointTest | 6 | 0 | 0 | 0 |
| **合计** | **46** | **0** | **0** | **0** |

与总监 01:25 XML（46/46）一致：net 17 + voice 29。

### 12.3 与 Task 6+7 的 27 用例衔接

- Task 6+7 的 27 用例 = net 17（RtcClientTest 10 + Playback 4 + RemoteAudioStatus 3）+ VoiceSessionCoordinatorTest 10，全部在本次复跑中重新执行且全绿。
- Task 8 新增 19 用例（VoiceUiModelTest 6 + BargeInControllerTest 7 + VoiceEntryPointTest 6）同样全绿。
- 46 = 27（Task 6+7）+ 19（Task 8），无重复、无缺失。

### 12.4 反作弊

- 7 个测试类源文件扫描：无 @Ignore / skip / .only / focus（v5 §11.4 已核 4 类；Task 8 三测试类本次抽读无标记）。
- XML 全量 0 skipped/0 failures/0 errors；`--rerun-tasks` 强制真实执行，排除 UP-TO-DATE 缓存嫌疑。

### 12.5 RoleVerdict（Task 6-8 最终）

```yaml
RoleVerdict_Task68:
  verdict: pass
  scope: Phase 3 Task 6+7+8（Android 串行生命周期、播放订阅解耦、VoiceUiModel 三入口）
  blocking: []
  resolved:
    - id: QA-T67-BLOCK-001  # 并发写 BLOCKED → 已闭环
      evidence: 前端 Task 8 提交后工作树稳定；QA 独立 --rerun-tasks 复跑
               compileDebugKotlin 通过 + 46/46 全绿。
  advisory:
    - VoiceEntryPointTest.kt:138 / VoiceUiModelTest.kt:136 有 String?→String 类型不匹配 warning
      （编译通过，测试断言仍成立），建议 Task 14 清理。
    - MainActivity.kt/SettingsActivity.kt 各 1 个 unused-parameter warning，非阻断。
```

---

## 13. Task 11 追加复核（v7，2026-08-08 01:50 本地）：本地隐私/加密转写/脱敏诊断

### 13.1 RoleVerdict

```yaml
RoleVerdict_Task11:
  verdict: pass
  scope: Phase 3 Task 11（隐私开关、OS-bound 加密转写、allowlist 诊断导出）
  blocking: []
  advisory:
    - TranscriptService.export 的 decrypt 在 destination.open 之后执行；若解密失败会留下
      空文件（无泄漏，但建议先解密后写文件，Task 14 顺手清理）。
    - MemoryKeyCipher 仅测试/无 OS 绑定开发使用（源码注释已声明），生产必须走 WindowsDpapiCipher。
```

### 13.2 三文件门禁

命令：
```bash
./.venv/Scripts/python.exe -u -m pytest backend/tests/unit/test_voice_privacy.py \
  backend/tests/unit/test_transcript_storage.py backend/tests/unit/test_redacted_diagnostics.py \
  -q -ra --tb=short -p no:cacheprovider
```
结果：**25 passed, 0 failed/skipped/xfailed**（16.23s）。用例构成 10+9+6=25 与源文件 @Test 计数一致。

### 13.3 实现抽读（真实实现）

| 文件（行数） | 核对 | 判定 |
|---|---|---|
| privacy.py（90） | 四开关 + persistence 由 PrivacyService 编排：先写 SQLite → runtime action → 失败回滚设置值（保持原值）+ 调动作 rollback；返回 {applied_at, effective_value, action_result}；默认动作开关 True、persistence False（AC-16） | ✅ |
| transcripts.py（183） | OsBoundKeyCipher ABC（encryption_version + encrypt/decrypt）；MemoryKeyCipher 注释"仅供测试/无 OS 绑定开发"；WindowsDpapiCipher 真实 win32crypt.CryptProtectData/CryptUnprotectData；save 未开启持久化返回 None 且零记录（AC-16）；delete 审计不含正文；export 仅用户路径 | ✅ |
| diagnostics.py（87） | DIAGNOSTIC_ALLOWLIST = 25 字段 frozenset（session_id/turn_id/state/error_code/.../duration_ms），build_redacted_diagnostic 按 allowlist 过滤（**非 denylist**）；scan_sensitive 对导出文本扫描；命中抛 DiagnosticLeakError 且**不写文件**（scan 先于 write） | ✅ |
| repositories/settings.py（26） | settings 表 get/set（unique(key) upsert）；storage.py 扩展后 282 行（≤300） | ✅ |

### 13.4 关键断言核对（测试源）

- 默认无正文：`test_default_no_persistence_creates_no_transcript_row`（save=None + rows==0 + DB 字节扫描无明文）
- DPAPI roundtrip：`test_windows_dpapi_cipher_roundtrip`（win32crypt 真实加解密）
- 删除不留副本：`test_delete_all_removes_rows_and_no_plaintext_copy`（删除后 DB + 审计表均无正文）
- 导出仅用户路径：`test_export_writes_only_to_destination`（目录内仅目标文件）+ `test_export_rejects_invalid_destination`（create_parents=False 父目录缺失抛 ValueError）+ `test_export_redacted_rejects_on_leak`（泄漏时 `not destination.exists()`）
- allowlist 契约：`test_allowlist_is_explicit_contract`（25 字段全在 + 敏感字段全不在）
- 四开关失败回滚：`test_action_failure_rolls_back_setting_value`（effective_value 回滚 + SQLite 回滚 + rolled_back 被调用）+ `test_action_failure_does_not_corrupt_other_settings`

### 13.5 全量回归与范围确认

- `pytest backend/tests -q -ra` => **467 passed, 0 failed/skipped/xfailed**（106.42s）。构成：438（Task 5 基线）+ 25（Task 11）+ 4（test_voice_legacy_gateway_fail_closed.py，Task 4 装配层测试，此前 advisory 建议项已由后端补齐）= 467。
- **未触碰 pet-ui/mobile-app**：pet-ui/src/components/Settings.tsx lastWrite 08/03、mobile-app SettingsActivity.kt lastWrite 08/06（均早于 Task 11）；Task 11 产物全部位于 backend/（privacy/transcripts/diagnostics + repositories/settings + 3 测试文件，lastWrite 08/08 01:45-01:47）。
- 反作弊：-ra 输出 0 skipped/xfailed；无 @Ignore/skip/.only/focus。

---

## 14. Task 9 追加复核（v8，2026-08-08 03:00 本地）：sidecar SDK 基线

### 14.1 RoleVerdict

```yaml
RoleVerdict_Task9:
  verdict: pass
  scope: Phase 3 Task 9（sidecar TRTC SDK 基线、真实注入契约、48k 假定清除）
  blocking: []
  advisory:
    - phone.js（显式联调角色）也含 enterRoom/sendCustomAudioData 调用点；sdk-smoke 测试
      明确允许（rtc.js 生产 + phone.js 联调），SPEC §4.3 的"唯一 TRTC adapter"指生产 sidecar
      路径，phone.js 属工具角色，可接受。
    - rtc.js 的 .env SecretKey fallback 仅限本地冒烟/联调（config.secretKeyFallback 门控），
      生产路径必须走签发端点；SPEC 生产禁止本地 SecretKey fallback（O-016 未决项保持）。
```

### 14.2 三命令复跑（QA 独立执行）

| 命令 | 结果 |
|---|---|
| `npm --prefix sidecar ls --depth=0` | `electron@31.7.7` + `trtc-electron-sdk@13.4.802-beta.3`，无 UNMET，EXIT=0 |
| `node scripts/verify-sidecar-sdk.js` | 6 项 OK（manifest/lock/require.resolve/installed/natives 1 个 .node 二进制），`verify-sidecar-sdk PASS`，EXIT=0 |
| `node --test sidecar/test/sdk-smoke.test.js sidecar/test/audio-contract.test.js` | **9/9 passed, 0 skipped/failed**（TAP：1 唯一构造点、2 d.ts 签名、3 640B 帧、4 SIGTERM 退出 6271ms 实测、5 日志无 Secret、6 包可解析、7 原生二进制、8 运行时真实版本、9 调用点受限），EXIT=0 |

### 14.3 实现抽读

| 文件（行数） | 核对 | 判定 |
|---|---|---|
| audio.js（120） | TRTCAudioFrame **唯一构造点**（audio.js:59 `new TRTCAudioFrame()`）；makeAudioFrame16k 设 audioFormat=1(PCM)/data/length/sampleRate=16000/channel=1/timestamp；帧字段注释引实际 d.ts（16k/24k/32k/44.1k/48k 支持，16k 直接注入无需重采样）；无 resample16kTo48k/makeAudioFrame48k 残留 | ✅ |
| rtc.js（287） | `cloud.sendCustomAudioData` 调用仅 rtc.js:81（bridge 下行回调，直接注入 640B 帧）+ rtc.js:245（E2E test_audio）；`enterRoom` 定义 rtc.js:61 + 调用 rtc.js:201；setAudioFrameCallback 远端帧 → frameToS16Mono16k（48k 多声道→16k 抽取，16k 原样直通）→ WS 上行 | ✅ |
| main.js | SIGTERM handler（12-15 行）：`app.exit(0)` 优雅退出；render-process-gone/unresponsive → app.exit(1) 让看门狗拉起；window-all-closed 保持常驻 | ✅ |
| logger.js | 通用模板 `[ts] [scope] msg`，无 Secret 字段；node 测试第 5 项"日志模板不含 Secret"实测通过 | ✅ |
| package.json | `trtc-electron-sdk: 13.4.802-beta.3`（未动）+ `electron: 31.7.7`（dev） | ✅ |

### 14.4 范围确认

- grep 全 sidecar 业务代码：`new TRTCAudioFrame` 仅 audio.js；`cloud.sendCustomAudioData(` 仅 rtc.js（生产）+ phone.js（联调角色，测试允许）；`resample16kTo48k`/`makeAudioFrame48k` **0 命中**。
- 未触碰 backend/mobile-app/pet-ui：Task 9 产物（audio.js 02:56 / rtc.js 02:51 / main.js 01:44 / verify-sidecar-sdk.js 01:42）全部位于 sidecar/ 与 scripts/；Settings.tsx（08/03）、MainActivity.kt（01:09）、privacy.py（01:45）均为其他批次时间戳。
- 反作弊：node --test 0 skipped/todo；无 @Ignore/skip。
