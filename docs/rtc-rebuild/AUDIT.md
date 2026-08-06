# AUDIT — RTC 重构 100 分制独立审计模板（波斯猫语音）

> 版本：v1.1（2026-08-06；v1.1：新增 §0.1 反作弊 diff 基准与回归基线记录，对接 git 基线 f6e9348）
> 作者：qa（测试工程师）
> 依据：docs/rtc-rebuild/QA-PLAN.md（v1.2 验收基准）、docs/rtc-rebuild/ARCHITECTURE.md（v1.0 实施契约）、docs/decisions/ADR-012-rtc-transport.md（Accepted）
> 红线：**没有审计过达到 100 分，都不能算完成；必须全员独立 QA/测试/审计/验证；不得靠模型自证。**

---

## 0.1 反作弊 diff 基准与回归基线（v1.1 新增）

- **反作弊 diff 基准 commit**：`f6e9348`（chore: baseline before RTC phase A，2026-08-06，用户配合建立）
  - 基线含 28 个 `backend/tests/unit/test_*.py` 文件；工作区已 0 未提交（git status 干净）。
  - 所有后续验收/反作弊检测（测试文件删除、断言数、skip 新增、测试框架配置篡改）以
    `git diff f6e9348 -- '*test*' pytest.ini pyproject.toml` 为基准。
- **回归基线**：`pytest tests/unit` = **324 passed**（2026-08-06 qa 独立重跑确认）。
  - 构成：原 294 基线 + be-pc 新增 `test_voice_session.py`(13) + QA 独立 `test_voice_session_qa.py`(15) + 其他新增 2。
  - relay 38 用例（test_relay_protocol/server/client_fake_dead/client_gateway_heartbeat）完整在库，未删除。
- **基线更新规则**：任何新增/删除测试必须走团队评审；删除/弱化/skip 新增触发 §6 反作弊门（P0 阻断）。

---


## 0. 审计总纲（一页结论）

- **审计人**：fe-mobile、be-pc、architect、qa + 项目总监（大湾区靓仔），共 5 人。**全员必须独立打分**，任何人不得代打、不得参考他人打分后修改。
- **评分规则**：满分 100 分，分 7 个维度。每维度独立打分，扣分项可叠加（扣至该维度 0 为止，不跨维度扣）。
- **取分规则**：**取 5 人每维度的最低分**，加总为最终分。即 `最终分 = Σ(各维度 5 人最低分)`。
- **通过条件**：**最终分 = 100 分**。任何维度被任何一名审计人扣分 → 最终分 < 100 → **退回重修**，修复后全员重新审计。
- **自证禁令**：实现方（fe-mobile / be-pc）不参与自己产出物的最终裁决打分自证；其打分仅作参考，**最终分以其他审计人的最低分为准**。实现方对自己模块须回避或显著保守（对自己产出扣分从严）。
- **审计时机**：每个 Phase（A/B/C）验收点前执行一次；Phase C 全量审计为上线最终门。

### 审计流程

```
1. team-lead 发布审计通知（附实现 diff 与测试 diff，分离评审）
2. 5 名审计人各自独立填《审计打分表》（docs/rtc-rebuild/audit-scores/ 下，每人一份，禁止查看他人）
3. qa 汇总：每维度取 5 人最低分 → 最终分
4. 最终分 = 100 → 通过，进入验收；最终分 < 100 → 打回 team-lead 分发修复，循环
5. 任何维度扣分项触发，必须在打回说明中列出具体缺陷 ID/证据，供修复人定位
```

---

## 1. 维度与分值

| # | 维度 | 满分 | 核心检查对象 |
|---|------|------|--------------|
| D1 | 契约一致 | 15 | 实现与 ARCHITECTURE/ADR-012 接口、回调名、数据流一致 |
| D2 | 安全 | 20 | SecretKey 不泄露、userSig 短时效、房间鉴权、传输加密 |
| D3 | 测试覆盖 | 20 | L0/L1/L2 三层齐备、变异加固、反作弊门通过 |
| D4 | UI 专业化 | 10 | 无 emoji 图标、无紫粉渐变、无 AI 模板味、六态不退化 |
| D5 | 回归 294 | 15 | 294 全绿基线、relay 38 用例显式迁移、只增不减 |
| D6 | 无自研残留 | 10 | relay/WS 客户端删除干净、无残留业务引用 |
| D7 | TRTC 官方文档遵循 | 10 | SDK 版本锁定、回调名对照官方、UserSig 算法正确 |
| **合计** | | **100** | |

---

## 2. 逐维度打分标准

### D1 契约一致（满分 15）

| 扣分项 | 扣分 | 说明 |
|--------|------|------|
| 手机端 RtcClient 进房参数与 ADR-012 不符（TRTCParams{sdkAppId,userId,userSig,strRoomId}、TRTCAppSceneAudioCall、startLocalAudio(SPEECH)） | 3 | 对照 ADR-012 决策 1 与 §5.1 |
| 手机端未实现 mic handoff（会话期 MicRecorder stop、TRTC SDK 独占；exitRoom 回调后再重启 MicRecorder） | 3 | ADR-012 §5.1/实施补充 |
| 手机端六态状态机 / VoiceUiState 与现有 VoiceState.kt 契约不兼容 | 3 | monitoring/listening/thinking/speaking/alerting |
| PC 端 RtcPeer 接口与 ARCHITECTURE §5.2 抽象不符（enter_room/exit_room/on_remote_audio/on_remote_user_enter/leave/on_connection_lost/recovery） | 3 | be-pc 契约 |
| sidecar↔Python 本地 WS 帧契约与 ARCHITECTURE §5.2 不符（up_audio/peer_state/down_audio/ctrl，16k s16 PCM） | 3 | 帧类型名、格式 |
| 会话签发接口 `<云函数>/api/v1/voice/session` 请求/响应字段与 ADR-012 不符（请求 device_id；响应 room_id/user_id/user_sig/sdk_app_id/scene，wire 层 snake_case；手机 userId=device_id；房间号 TRTC_ROOM_PREFIX+device_id） | 3 | 契约不一致（ADR-012 决策 7，ARCHITECTURE §3.4） |

> 注：本维度扣分项可能重叠（如接口没落地则多项同时扣），按实际逐项扣。

### D2 安全（满分 20）

| 扣分项 | 扣分 | 说明 |
|--------|------|------|
| SecretKey 出现在手机 App / 前端 / 测试 / 日志 / 上报 / PC .env 生产路径 | 5（P0） | SecretKey 唯一存云函数环境变量（TRTC_SECRETKEY）；grep 扫描确认 |
| userSig 有效期 > 600s（10min） | 3 | ADR-012 建议 ≤10min；QA-PLAN §6.3 |
| userSig 由前端/客户端本地签发（未走云函数/服务端） | 4（P0） | 必须云函数服务端签发（ARCHITECTURE §3.4） |
| 房间无鉴权（任意陌生端可进房收流） | 3 | 房间越权测试（QA-PLAN §6.3） |
| RTC 传输未启用加密 / 未确认 SDK 默认加密 | 2 | DTLS-SRTP 确认 |
| 房间号可枚举 / 固定（无 userSig 房间鉴权） | 2 | roomId 规则 TRTC_ROOM_PREFIX+device_id（定稿）；防枚举依赖 userSig 鉴权，非房间号不可猜 |
| 日志/上报泄露 token/roomId/userSig 明文 | 2 | grep 日志代码 |
| 免费额度限额无防护/无告警 | 1 | R2 缓解项 |

### D3 测试覆盖（满分 20）

| 扣分项 | 扣分 | 说明 |
|--------|------|------|
| 无 L0 单测（PC RtcPeer + 手机 RtcClient mock 测试缺失） | 4 | QA-PLAN §4.1 清单 10 项 |
| 无 L1 集成（本地双端回环 / RTC+apm_bridge 集成） | 4 | QA-PLAN §4.2 至少一条真云路径 |
| 无 L2 真机/跨网验收或未执行 | 3 | QA-PLAN §5 |
| 关键逻辑无变异加固（重连退避/离线清理/格式转换/上行门控） | 3 | QA-PLAN §4.1 变异点，注入变异必红 |
| 测试完整性反作弊门任一触发（测试删/断言降/skip 新增/硬编码/配置篡改） | 4（P0） | QA-PLAN §6，任一即本维度扣 4 且阻断 |
| 新测试全部 mock 无真云路径 | 2 | 幻觉依赖防线（QA-PLAN §4.3） |
| 测试断言绑实现输出（非 Spec 定义值） | 2 | 人工审查 |

### D4 UI 专业化（满分 10）

| 扣分项 | 扣分 | 说明 |
|--------|------|------|
| emoji 作为 UI 功能图标 | 4（P0） | 扫描 mobile-app/ backend/ |
| 紫色→粉色渐变 | 2 | 扫描 CSS/kt/xml |
| AI 模板味文案（Welcome to / Lorem ipsum 等） | 2 | 扫描 mobile-app/ backend/ |
| 六态 UI / 悬浮窗在 RTC 化后退化（状态与音频不一致） | 2 | QA-PLAN §3.3 |

### D5 回归 294（满分 15）

| 扣分项 | 扣分 | 说明 |
|--------|------|------|
| `pytest tests/unit` 总数 < 294 | 5（P0） | 只增不减 |
| 与 relay 无关的 256 用例任一被改动/删除 | 5（P0） | git diff 核对 |
| relay 38 用例未显式迁移（静默删除） | 4（P0） | 迁移为 RTC 对端等价用例（进房/退房/重连/对端离线/心跳超时） |
| 迁移后新增 skip/xfail/.only 掩盖失败 | 4（P0） | 反作弊门第 3 项 |
| 由绿转红回归率 ≠ 0（未消回归） | 3 | QA-PLAN §6.1 一等指标 |

### D6 无自研残留（满分 10）

| 扣分项 | 扣分 | 说明 |
|--------|------|------|
| backend/relay/ 未删除 | 3 | ARCHITECTURE §4.1 删除清单 |
| deploy/relay/ 未删除 | 2 | 同上 |
| 手机端 VoiceWsClient/FrameCodec/PairFrame/VoiceCipher 未删除 | 3 | 同上 |
| `grep -rn "relay" backend/ mobile-app/` 有业务残留引用（文档/注释除外） | 2 | Phase C 验收点 |
| ws_server.py 局域网直连保留（未统一走 TRTC） | 2 | ADR-012 决策 4 已裁决：删除（routes_voice.py 的 /ws/voice、/api/v1/voice/stream、/api/v1/voice/pair） |

### D7 TRTC 官方文档遵循（满分 10）

| 扣分项 | 扣分 | 说明 |
|--------|------|------|
| SDK 版本未锁定精确版本（Android LiteAVSDK_TRTC 13.4 / sidecar trtc-electron-sdk） | 3 | ADR-012 决策 6；禁 latest.release |
| 回调名与官方不符（onTryToReconnect 非 onTryReconnect、onRemoteUserAudioStatus、onUserVoiceVolume、onConnectionLost/Recovery） | 3 | ADR-012 实施补充；以锁定的 SDK jar 为准 |
| TRTCParams 使用错误（strRoomId 与 intRoomId 同时非 0） | 2 | ADR-012 实施补充 |
| UserSig 算法与官方 GenUserSig 不符（TLS.version:201512300000、HMAC-SHA256、base64 JSON 结构） | 3 | 独立验签测试 |
| 版本/回调名核验无记录（fe-mobile 未回写 ADR） | 2 | 幻觉依赖防线 |

---

## 3. 审计打分表模板

每位审计人独立填写一份，存放 `docs/rtc-rebuild/audit-scores/AUDIT-<审计人名>.md`，**审计期间禁止查看他人打分**。

```markdown
# 审计打分 — <审计人名> — <日期>

> 本人独立完成，未参考他人打分。实现方对自己产出从严。

| 维度 | 满分 | 我的得分 | 扣分项命中（ID+证据） |
|------|------|----------|----------------------|
| D1 契约一致 | 15 |  |  |
| D2 安全 | 20 |  |  |
| D3 测试覆盖 | 20 |  |  |
| D4 UI 专业化 | 10 |  |  |
| D5 回归 294 | 15 |  |  |
| D6 无自研残留 | 10 |  |  |
| D7 TRTC 官方文档遵循 | 10 |  |  |
| **合计** | **100** |  |  |

### 发现的缺陷清单
| ID | 级别(P0/P1/P2) | 描述 | 维度 | 证据 |
|----|----------------|------|------|------|
```

---

## 4. 汇总规则（qa 执行）

1. 收集 5 份打分表（fe-mobile / be-pc / architect / qa / 总监）。
2. 每维度取 5 人中的**最低分**：`D_i_final = min(D_i_fe, D_i_be, D_i_arch, D_i_qa, D_i_lead)`。
3. `最终分 = Σ D_i_final`。
4. 判定：
   - `最终分 == 100` → 通过，进入验收门禁（QA-PLAN §10）。
   - `最终分 < 100` → **退回重修**，qa 汇总输出《打回报告》：逐维度列出扣分项与缺陷 ID，交 team-lead 分发修复；修复后全员重新审计。
5. 汇总表输出：

```markdown
## 审计汇总 — <日期> — 第 N 轮

| 维度 | fe-mobile | be-pc | architect | qa | 总监 | 取最低 |
|------|-----------|-------|-----------|-----|------|--------|
| D1 |  |  |  |  |  |  |
| ... |  |  |  |  |  |  |
| **合计** |  |  |  |  |  | **最终分** |

**结论：通过 / 退回重修（<100）**
**打回原因摘要：** ...
```

---

## 5. 反自证约束（红线落实）

- **禁止**：实现方用「我跑过测试全绿」或「我自己验证过」作为通过证据。
- **强制**：最终分必须由独立审计人（非实现者）给出；qa 对实现方的测试结果做交叉复核（独立重跑、独立验签、独立扫描）。
- **独立验签**：userSig 算法正确性由 qa 用独立实现（测试内自写验签器）验证，不依赖 be-pc 的 GenUserSig 自证。
- **独立扫描**：emoji/渐变/模板味/relay 残留/SecretKey 泄露由 qa 独立 grep 扫描确认。

---

## 6. 审计与验收门禁的衔接

```
AUDIT 100 分通过（本文件）
  ↓
QA-PLAN §10 验收门禁顺序执行
  0. 反作弊门 → 1. 冒烟 → 2. 回归 ≥294 → 3. 全双工回归 → 4. RTC 新测试
  → 5. 稳定性 → 6. 跨网真机 → 7. 安全/视觉/失效模式 → 8. 生产就绪 ≥ Silver → 9. 质量报告
  ↓
上线建议：P0=0 且 P1≤20% 且 回归率=0 且 总档≥Silver
```

> 审计通过 ≠ 可直接上线；审计是「实现与契约/红线一致」的门，性能/真机/生产就绪仍走 QA-PLAN 门禁。
