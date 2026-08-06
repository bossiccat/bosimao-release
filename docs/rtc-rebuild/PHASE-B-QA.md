# PHASE-B-QA — Phase B 验收准备（电脑端 RTC 对端 + 云函数代签 + 本地回环）

> 版本：v1.0（2026-08-07）
> 作者：qa-2（测试工程师，独立验收）
> 依据：docs/rtc-rebuild/QA-PLAN.md（v1.2 验收基准）、docs/rtc-rebuild/ARCHITECTURE.md（§3.4 云函数代签 + 进房协调、§5.2 sidecar/rtc_bridge）、docs/decisions/ADR-012-rtc-transport.md
> 范围：Phase B（云函数 trtc-sign 代签 + PC sidecar 收手机音频 + rtc_bridge 接 apm_bridge→MiniCPM-o 全双工 + 本地双端回环联调）的验收准备；跨网真机验收（手机深圳↔PC 衡阳）待 Phase B 完成后执行
> 红线：**没有审计达到 100 分不算完成；独立验证不许自证。**

---

## 0. 一页结论（Phase B 验收门）

Phase B 完成后，按以下顺序验收（衔接 QA-PLAN §10）：

| # | 门禁 | 判定 | 本文件对应 |
|---|------|------|-----------|
| B0 | 测试完整性反作弊门 | 无测试删/弱化/skip/硬编码/配置篡改；实现 diff 与测试 diff 分离 | §6 |
| B1 | L1 集成（真 RTC 云本地回环） | G1 音频到达 sidecar（PCM 帧>0）**必过**；G2/G3 全链路（rtc_bridge→MiniCPM-o 回复回传 >0 字节）必过 | §1 |
| B2 | 云函数代签验证 | 手机直调云函数拿 userSig → 进房成功；PC 轮询意图进房 ≤2s；SecretKey 不进 PC/手机/repo | §2 |
| B3 | 跨网真机验收（Phase B 后） | 10 步清单全过（§3），P0=0 | §3 |
| B4 | 指标验收 | 首音 P50 ≤2s / P95 ≤3s；30min 稳定；打断 <500ms（P50）；静默回落 15s | §4 |
| B5 | 风险复核 | 云函数冷启动/免费额度/sidecar 看门狗三项缓解到位 | §5 |
| B6 | 生产就绪 | 七维记分卡总档 ≥ Silver（QA-PLAN §7 不变） | §7 |

> 冒烟门（Phase B 完成后 30 分钟内）：本地回环 G1 过（进房+收帧）→ 真机手机进房→说话→听到回复→退房。任一不通直接打回。

---

## 1. L1 集成测试设计（本地双端回环连真 RTC 云）

### 1.1 被测链路

```
mock 手机（Electron 渲染进程 qa-phone，注入 WAV）
  → TRTC 云（SDKAppID=1600155678，room=jax-<device_id>）
  → sidecar（trtc-electron-sdk 13.3.801，userId=jax-pc-sidecar）
  → [rtc_bridge 本地 WS :19092] → ApmBridge → MiniCPM-o Realtime API
  → 回复音频经 TRTC 下行 → mock 手机收到（>0 字节）
```

### 1.2 断言（Gate）

| Gate | 断言 | 测量点 | 通过标准 |
|------|------|--------|----------|
| G1 | 音频到达 sidecar | sidecar 日志 `远端音频帧 userId=` 计数（onPlayAudioFrame） | **PCM 帧数 > 0**（必过，传输真实性） |
| G2 | rtc_bridge 转发 apm_bridge | bridge 进程收到 up_audio 且喂给 ApmBridge（bridge 侧打点） | up_audio 转发成功（bridge 侧日志） |
| G3 | MiniCPM-o 回复回传 mock 手机 | qa-phone 日志 `FINAL downBytes=` | **下行音频字节 > 0** |

### 1.3 可执行脚本

**脚本：`tmp/phase_b_l1_test.py`**（已交付，v1.0）

- 运行时生成 `sidecar-smoke/qa-phone/`（mock 手机注入渲染器：进房 + `enableCustomAudioCapture` + `sendCustomAudioData` 注入 `tmp/poc_b3_ask_16k.wav` 16k mono s16 + `onPlayAudioFrame` 统计下行字节），不污染既有 `rtc-renderer.js`。
- PC 侧复用 `sidecar-smoke/rtc-renderer.js`（Phase A 产物，userId=jax-pc-sidecar）。
- 退出码：0=全 PASS / 1=有 FAIL / 2=有 SKIP（前置缺失）/ 3=超时进程异常。

```bash
cd "C:/Users/Administrator/WorkBuddy/监视app"
.venv/Scripts/python.exe tmp/phase_b_l1_test.py --gate g1 --device l1-qa-01 --hold 90   # 只跑 G1
.venv/Scripts/python.exe tmp/phase_b_l1_test.py --gate all --device l1-qa-01 --hold 120 # G1+G2+G3
.venv/Scripts/python.exe tmp/phase_b_l1_test.py --cleanup                                # 清理生成的 qa-phone/
```

**执行条件：**
- 项目根 `.env` 有 `TRTC_SDKAPPID` / `TRTC_SECRETKEY` / `TRTC_ROOM_PREFIX`（已确认存在）。
- G1：仅需 sidecar-smoke + WAV，**Phase B 组件未就绪也能跑**（先验传输真实性）。
- G2/G3：需 `rtc_bridge.py` 监听 `ws://127.0.0.1:19092`（Phase B be-pc 交付）；未就绪时脚本如实报告 **SKIP**，不伪造通过。

### 1.4 反作弊约束（本脚本）

- 断言值来自**真 RTC 云实际回传的帧/字节数**，不来自 be-pc 实现返回值；
- mock 手机是独立渲染进程（非被测 PC 侧代码自证）；
- G2/G3 前置缺失只报 SKIP，不降级断言换绿。

---

## 2. 云函数代签验证（trtc-sign）

对齐 ARCHITECTURE §3.4 / ADR-012 决策 #7。Phase B 交付后执行：

| # | 检查项 | 通过标准 | 证据 |
|---|--------|----------|------|
| S1 | 手机直调云函数 | `POST <云函数>/api/v1/voice/session`（body `{device_id}`）返回 `{room_id, user_id, user_sig, sdk_app_id, scene:"audio_call"}`，snake_case | 响应 JSON + 日志 |
| S2 | userSig 时效 | user_sig 解析后 `TLS.expire ≤ 600`；过期后进房被拒 | qa 独立验签器（backend usersig.py `parse_user_sig`/`user_sig_expire_ok`） |
| S3 | room_id 规则 | `room_id = TRTC_ROOM_PREFIX + device_id`（`jax-<device_id>`），同 device 幂等 | 两次请求 room_id 相同 |
| S4 | 设备白名单 | 未注册 device_id → 拒绝（错误码） | 400 响应 |
| S5 | **SecretKey 唯一性** | SecretKey 仅存云函数环境变量；PC .env 生产路径 `TRTC_SECRETKEY` **置空**；手机 App 不持有 | qa 独立 grep：PC 生产路径/手机代码/repo/日志无 SecretKey 明文 |
| S6 | PC 轮询进房 | PC 常驻轮询 `GET /session/pending` → 发现意图 ≤2s 内 `POST /sign` 取自身 userSig → sidecar 进同房 | PC 侧日志时间戳（意图 ts → 进房 ts） |
| S7 | 手机先入房等待 | 手机先入房 → PC 随后加入 → 两端互见 | 进房日志 + 远端加入日志 |

> 反自证：userSig 验签用 qa 独立实现（backend `usersig.py` 的 parse 函数），不依赖 be-pc 自证。

---

## 3. 跨网真机验收清单（Phase B 完成后执行，手机深圳 4G ↔ PC 衡阳 NAT 后）

> 前置：手机装 v0.6.0-rtc APK；PC 起 sidecar + rtc_bridge + backend；云函数已部署；两端时钟对齐；准备录屏/日志采集。

| # | 步骤 | 通过标准 | 证据 |
|---|------|----------|------|
| 1 | 手机装 v0.6.0-rtc，设置页填云函数地址，保存 | 地址持久化；重启 App 仍保留；状态页显示 rtc_status=connected / phone=online | 设置页截图 + 状态页截图 |
| 2 | 手机 KWS 唤醒（"贾克斯"） | 唤醒命中 → 悬浮窗切 listening → 自动进房（进房成功日志/UI） | 手机日志 `enterRoom` + 悬浮窗截图 |
| 3 | 说话："介绍一下你自己" | 听到完整语音回复；首音 P50 ≤2s（手机端打点） | 打点数据（speech_end→播放首音） |
| 4 | PC sidecar 日志确认 | sidecar `onEnterRoom` 成功 + `onRemoteUserEnterRoom` 见手机 + `onPlayAudioFrame` 帧数>0；rtc_bridge 日志见 up_audio 转发 | PC 日志行（时间戳 + 帧计数） |
| 5 | 回复音频听到 | 手机扬声器听到 MiniCPM-o 回复；无爆音/卡顿 | 录音回放 + 卡顿计数 |
| 6 | 打断测试（说话中插话） | Speaking 中插话 → 500ms 内停播切 Listening → 模型原生 barge-in 续答 | 打断打点（本地采集能量阈值参考 + SDK 播放状态/状态机切态，非本地 VAD） |
| 7 | "退下" 退房 | 语音指令触发退房 → 状态回 monitoring → sidecar 收到远端离开 → 退房回待命 | 手机状态机日志 + PC `onRemoteUserLeaveRoom` |
| 8 | 重复会话 | 再次唤醒→进房→对话→退房，连续 3 次全成功，无残留旧会话（上一轮回答不重播） | 3 轮日志 + 播放无残留 |
| 9 | 断网恢复 | 通话中手机 WiFi→4G：自动重连 P50 ≤5s；恢复后直接继续对话（无需重新唤醒/提问） | 重连打点 + 恢复后对话成功 |
| 10 | 30min 稳定（可并入） | 30min 内 0 次非预期断开；无爆音卡顿；PC 内存增长 ≤100MB | 断线计数 + RSS 起止对比 + 录音抽查 |

**通过标准汇总：** 10 步全过且无 P0；P1 ≤ 总数×20%；§4 指标全达标；生产就绪 ≥ Silver。

---

## 4. 指标验收（测量方法与记录表）

### 4.1 端到端首音延迟（说话结束 → 手机听到回复首音）

| 项 | 值 | 约束 |
|----|-----|------|
| 目标 | **P50 ≤ 2.0s / P95 ≤ 3.0s** | 硬门（QA-PLAN §1.1） |
| 打点位置 | **手机端**：`speech_end`（VAD 说完）→ 播放线程输出首音（非首包，防缓冲虚增） | 不允许 PC 侧日志自证 |
| 采样 | ≥ 20 轮对话 | 含 4G、WiFi、跨网 |
| 测量辅助 | 本地回环 G1 也可打点（注入结束→收首帧），作为真机前的预检 | 不替代真机口径 |

**记录表（真机 ≥20 轮）：**

| 轮次 | 网络 | speech_end ts | 首音 ts | 延迟(ms) | 备注 |
|------|------|---------------|---------|----------|------|
| 1 | 4G | | | | |
| … | | | | | |
| 20 | WiFi | | | | |
| **P50 / P95** | | | | **≤2000 / ≤3000** | |

### 4.2 会话 30min 稳定

| 指标 | 目标 | 测量方法 |
|------|------|----------|
| 30min 内非预期断开 | **0 次** | 手机 RTC 状态回调（disconnected/reconnecting/connected）计数；join 状态不丢 |
| 播放卡顿（buffer underrun） | ≤10 次/30min | 播放线程 underrun 计数 + 录音人工听测（MOS ≥3.5） |
| PC 内存增长 | ≤100MB | RSS 起止采样 |
| 手机功耗（可选） | ≤5%/30min | 电量打点，记录机型 |

### 4.3 打断响应（barge-in）

| 项 | 值 | 约束 |
|----|-----|------|
| 目标 | **< 500ms（P50）** | 硬门（PRD V-3） |
| 口径（对齐 ARCHITECTURE §5.1 mic handoff） | 开口参考=本地采集回调能量阈值打点（**仅测试度量，不参与控制**）；停止证据=**SDK 播放状态 + 状态机切态（Speaking→Listening）** | 不依赖本地 VAD 控制打断 |
| 采样 | ≥ 10 次 | 含连续打断 3 次场景 |

**记录表（≥10 次）：**

| # | 开口参考 ts | 停播/切态 ts | 响应(ms) | 续答正常？ |
|---|-------------|--------------|----------|-----------|
| 1 | | | | |
| … | | | | |

### 4.4 静默回落

| 项 | 值 |
|----|-----|
| 目标 | 15s 无对话 → 状态回 monitoring（PRD V-5） |
| 测量 | 手机六态状态机打点（listening/thinking→monitoring 时间） |

---

## 5. 风险更新（Phase B 新增 / 复核）

| # | 风险 | 影响 | 缓解/验证 | 验收动作 |
|---|------|------|-----------|----------|
| R11 | **云函数冷启动延迟** | 手机首次唤醒 → 直调云函数 `POST /session` 有冷启动（首次调用需拉起运行时，实测预期 500ms–2s，需真测）→ 叠加进房延迟可能挤占首音预算 | ① 实测冷启动 vs 热调用各 5 次，记录 P50/P95；② 若 >1s 影响首音，评估 keep-warm（定时触发）或预创建实例；③ userSig expire ≤600s 不受冷启动影响（签发本身毫秒级） | 本文件 §4 记录表附「session 接口延迟」列；S1/S2 验收时实测 |
| R12 | **RTC 免费额度（10k 分钟/月 × 第一年，1v1 计 2×）消耗监控** | 额度用尽中断或产生费用；联调/验收期大量回环+真机测试会额外消耗 | ① 仅会话期进房（常驻监听不消耗）；② 本地回环 L1 每次 hold 1-2min × 2 端计 2× 时长，需记入测试用量账；③ 控制台用量页定期核对 + 额度告警 | 验收时记录 TRTC 控制台当前剩余额度；估算月用量（预估日常 ~1800min/月，测试期另计） |
| R13 | **sidecar 进程崩溃恢复（看门狗）** | sidecar（Electron）崩溃 → 手机音频无人收 → 会话中断 | ① rtc_bridge 心跳：sidecar 每 5s 上报，Python 超时 30s 重启（ARCHITECTURE §5.2 / PC-INTEGRATION §2.2）；② jax-watchdog.ps1 检测 /health 拉起；③ 崩溃恢复后需自动重新进房（同 room_id 幂等），手机不感知或短暂提示 | 验收场景：kill sidecar 进程 → 观察 30s 内重启 + 重进房 + 手机继续对话成功 |
| R14 | **PC .env 置空后本地回环需临时签发** | Phase B 生产路径统一云函数，PC .env TRTC_SECRETKEY 置空；但本地 L1 回环仍依赖本机签发 | L1 脚本明确标注为**测试专用**（读项目根 .env 临时签发），仅限本机联调，不进入生产路径；真机验收走云函数 | §2 S5 确认 PC 生产路径置空 + 本地测试签发与生产路径隔离 |
| R15 | **trtc-electron-sdk 版本互通** | sidecar 13.3.801（npm 最新线）与手机 LiteAVSDK_TRTC 13.4 同大版本线，互通应无碍但需实测 | R1 冒烟已确认 sidecar 进房/远端互见；L1 真云回环进一步验证跨小版本互通 | G1 进房+互见+收帧实测 |
| R16 | **G2/G3 依赖 MiniCPM-o 在线** | 全链路断言依赖云端 Realtime API 可用 | G2/G3 前置探测 bridge WS；API 不可达时如实 SKIP，不降级通过；L1 前先跑 QA-PLAN §3.2 全双工回归确认能力资产未退化 | 验收时记录 API 可用性 |

---

## 6. 测试完整性反作弊门（Phase B 专项）

Phase B 交付后逐项执行（对齐 QA-PLAN §6 / AUDIT 反自证约束）：

| # | 检测项 | 方法 | 阻断条件 |
|---|--------|------|----------|
| 1 | 测试文件/用例被删 | `git diff --name-status HEAD~1 -- '*test*'` | 出现 `D` 且无迁移记录（relay 38 用例必须显式迁移） |
| 2 | 断言数下降 | 对比开发前后 `expect(/assert ` 数量 | 总断言数下降（排除等价迁移） |
| 3 | 新增 skip/xfail/.only/focus | `git diff HEAD~1 -- '*test*' | grep '^+' | grep -i 'skip\|xfail\|\.only\|focus'` | 任何新增 |
| 4 | 硬编码断言 | 人工审查：断言值来自 Spec 而非实现返回值 | 断言绑实现输出 |
| 5 | 配置篡改 | `git diff HEAD~1 -- pytest.ini pyproject.toml build.gradle.kts` | coverage 阈值调低 |
| 6 | 实现/测试 diff 未分离 | 提交记录检查 | 同 commit 混入测试改动且无说明 |
| 7 | **SecretKey 泄露扫描** | qa 独立 grep `TRTC_SECRETKEY` 值/明文于 repo/日志/PC 生产路径/手机代码 | 任何泄露 = P0 |
| 8 | **emoji/渐变/模板味** | 视觉合规扫描（QA-PLAN §6.2）覆盖新增 RTC UI | 发现 emoji 作图标 = P0 |

---

## 7. 生产就绪记分卡（Phase B 验收时评级，总档 ≥ Silver）

对齐 QA-PLAN §7，Phase B 关注点：

| 维度 | Silver 达标要点（Phase B） | 证据 |
|------|----------------------------|------|
| 测试+回归 | L1 全链路过；回归率=0；回归集进门禁 | §1 结果 + §6 反作弊 |
| 契约 | rtc_bridge/sidecar WS 契约、云函数会话契约（snake_case）先定义且测试对齐 | §2 S1/S3 + 契约测试 |
| 安全 | SecretKey 唯一存云函数环境变量；房间鉴权；限额监控 | §2 S5 + §5 R12 |
| 无障碍 | 语音场景非重点（Bronze） | — |
| 性能 | 首音/打断/30min 内存达标；音频转发无逐帧阻塞 IO | §4 |
| 可观测 | sidecar/bridge 结构化日志 + 端到端延迟/断线/重连指标 | §3 证据列 |
| 发布安全 | sidecar 看门狗 + 云函数回滚预案 | §5 R13 |

**总档 = 各维最低档。未达 Silver 不交付商业生产。**

---

## 8. 回归集产物（Phase B 预埋）

Phase B 验收中若发现以下缺陷，修复后必须沉淀为持久回归用例（进 `tests/regression/`）：

- `test_rtc_sidecar_crash_recover.py`（sidecar 崩溃 30s 内重启重进房）
- `test_cloudfunc_cold_start_latency.py`（云函数 session 接口延迟阈值）
- `test_rtc_quota_monitor.py`（免费额度用量记录/告警）
- `test_rtc_uplink_forward_no_gate.py`（上行持续转发无轮次门控，native barge-in 前提，QA-PLAN §4.1 #5 变异点）

---

## 9. 验收门禁总表（Phase B 完成后 qa-2 执行顺序）

```
0. 测试完整性反作弊门（§6）→ 任一作弊 → P0 阻断打回
1. L1 集成：python tmp/phase_b_l1_test.py --gate all → G1/G2/G3 全 PASS（G1 必过，G2/G3 不允许 SKIP）
2. 云函数代签验证（§2 S1-S7）
3. MiniCPM-o 全双工回归（QA-PLAN §3.2：双轮/打断/停顿判定）
4. 后端单测回归 ≥ 324（QA-PLAN §3.1；relay 38 用例显式迁移）
5. 跨网真机验收 10 步清单（§3）
6. 指标验收（§4）：首音 P50/P95、30min、打断 <500ms、静默回落
7. 风险复核（§5 R11-R16）
8. 生产就绪评级（§7）→ 总档 ≥ Silver
9. 输出质量报告 + 缺陷清单 + 回归集更新
```

**上线建议判定：** P0=0 且 P1 ≤ 总数×20% 且 回归率=0 且 总档 ≥ Silver → 通过；否则不通过。
