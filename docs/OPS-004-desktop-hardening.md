# OPS-004: 桌面端加固四件套（服务管理 / watchdog 自愈 / 计划任务 / 中继假死感知）

> 状态：**已落地（2026-08-05 卜宕机实测）** | 目标：解决桌面端"经常出问题"三大机制
> 1. 云端中继实例假死（health 200 但 WS 业务卡死）→ 本地 watchdog 检测 + relay_client 假死感知退避
> 2. 后端/relay_client 进程被沙箱/会话回收 → 计划任务开机自启 + 每 5 分钟自愈
> 3. 端口残留/TIME_WAIT 冲突 → PID 文件管理 + 按端口定位不盲杀 + 幂等启动
> 前置：OPS-002（云端中继）/ OPS-003（真机链路装配）已上线

---

## 1. 一键操作（用户视角，PowerShell 5.1）

所有命令在项目根目录 `C:\Users\Administrator\WorkBuddy\监视app` 下执行：

```powershell
# 查看三件套状态（模型 :19080 / 后端 :8000 / relay_client 公网桥接）
powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 status

# 一键启动三件套（幂等：已在运行的健康服务自动跳过）
powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 start

# 一键停止三件套
powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 stop

# 强制重启三件套（杀旧进程后重启；用于端口残留/进程卡死）
powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 restart
```

单服务操作（`model` / `backend` / `relay`）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 start relay      # 只拉 relay_client
powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 restart relay    # 重启 relay（清多实例残留）
powershell -ExecutionPolicy Bypass -File scripts/jax-services.ps1 status
```

### status 输出示例

```
=========== 贾克斯三件套状态 ===========
[model]   :19080    OK
[backend] :8000     OK
[relay]   wss-relay RUNNING(x1)  PID=7276
========================================
```

含义：
- `OK`：端口监听 + /health 200
- `PORT-NOHEALTH`：端口被占但健康检查不过（疑似残留/半死进程）
- `DOWN`：未运行
- `RUNNING(xN)`：relay_client 顶层实例数（N>1 时提示残留，建议 restart relay）

---

## 2. 自愈机制说明

### 2.1 三件套与 PID 文件

| 服务 | 端口/端点 | PID 文件 | 启动命令 |
|---|---|---|---|
| model | :19080/health | data/pids/model.pid | llama-server.exe（B1 锁定 --ctx-size 4096） |
| backend | :8000/health | data/pids/backend.pid | .venv python -m uvicorn app.main:app --port 8000 |
| relay | 无端口（客户端） | data/pids/relay.pid | .venv python -m backend.relay.relay_client --relay 公网 --gateway ws://127.0.0.1:8000/ws/voice |

- 每次 Start-Process 后把返回的 PID 写入 `data/pids/*.pid`；
- **幂等**：start 前先查端口/进程，已有健康服务则跳过；跳过时回填现有进程 PID 保持文件一致；
- **不盲杀**：启动前发现端口被占但健康不过，只报告不杀；stop 时若 PID 文件失效，按端口 + 命令行白名单（llama-server/uvicorn/relay_client）定位再停。

### 2.2 watchdog 自愈（scripts/jax-watchdog.ps1）

由计划任务触发，**单次运行**逻辑：
1. 检查三件套健康（model/backend 走 /health；relay 走"进程存在 且 未处于假死错误循环"）
2. 哪个挂了 → 调用 `jax-services.ps1 start <svc>` 自动拉起
3. 动作写 `logs/watchdog.log`（时间戳 + 动作 + 结果）；**静默成功不写日志**

**中继假死检测**：扫描 `logs/relay_client.log(.err)` 最近 5 分钟——
- 若几乎全为 `relay event: error` / `relay connect failed` / `relay loop end`（≥3 行错误且无配对/注册/心跳迹象）→ 判定中继连接异常 → 重启 relay_client（重启会重新连中继，新实例就绪即恢复）

**防风暴**：每服务 10 分钟内最多重启 3 次（记录于 `data/pids/.watchdog_state.json`），超限写告警不再拉起，避免中继假死期间疯狂重启。

### 2.3 计划任务（scripts/install-scheduled-tasks.ps1）

| 任务名 | 触发 | 作用 |
|---|---|---|
| Jax-Watchdog-AtStartup | 开机（AtStartup） | 开机即自愈 |
| Jax-Watchdog-Every5Min | 每 5 分钟（RepetitionInterval PT5M） | 持续自愈 |

- 注册幂等：先 Unregister-ScheduledTask 同名任务再注册；
- 用 Register-ScheduledTask cmdlet（不用 schtasks）；
- 每 5 分钟任务 RepetitionDuration 用 3650 天（Task Scheduler 合法上限；`[TimeSpan]::MaxValue` 会产生非法 XML 导致注册失败——已踩坑修复）。

### 2.4 relay_client 假死感知（backend/relay/relay_client.py，Task4）

问题：中继假死时"WS 连上但 pair 无响应"场景每 15s 空转重试、日志 `relay event: error` 无限循环。

优化（保持原有断线自动重连不变）：
- 连接建立后，若 20s 内收不到中继**任何**响应（健康中继每 15s 发 heartbeat ping，20s > 15s 必能收到；收到 paired/peer_left/error 也算响应）→ 记一次 pair timeout；
- 连续 **3 次** pair timeout → 判定中继实例假死 → 日志明确：
  `relay instance suspected dead (pair timeout x3), backoff 60s`
- 假死退避 60s（`_suspect_dead_until`），重连前等待剩余退避时间；
- 收到任何中继消息 → 计数复位（中继业务存活）；
- 对应单测：`backend/tests/unit/test_relay_client_fake_dead.py`（5 例）。

---

## 3. 常见故障速查表

| 症状 | 判断 | 处理 |
|---|---|---|
| 手机连不上、中继 health 200 但无回传 | 云端中继实例假死（health 不感知 WS 业务） | 本地 `restart relay`；云端控制台重启 jax-relay 服务（或等 watchdog 自动拉起）；验证 relay_client 日志出现 `relay paired` |
| relay_client 日志无限 `relay event: error` / `relay connect failed` | 中继假死或网络/代理异常 | 已加固：连续 3 次自动退避 60s + watchdog 重启；手动 `restart relay`；确认代理 `HTTP_PROXY/HTTPS_PROXY=127.0.0.1:7890` 未破坏（relay_client 已 `proxy=None`） |
| `[relay] RUNNING(x2)` 或多个 relay_client | 多实例残留互相抢占配对码（中继 max_sessions_per_code=1） | `restart relay`（Stop-AllRelay 杀启动器+子进程整对） |
| 端口 8000 被占、新后端起不来 | 旧进程残留 / TIME_WAIT | `restart backend`；仍不行 `netstat -ano | findstr :8000` 手动确认占用进程 |
| watchdog.log 出现"10 分钟内已重启 3 次，跳过拉起" | 防风暴生效（10min 内 3 次重启上限） | 属正常保护；若持续频繁重启说明服务/中继真有问题，查看对应日志 |
| 开机后三件套没起来 | 计划任务未注册/被禁用 | `powershell -ExecutionPolicy Bypass -File scripts/install-scheduled-tasks.ps1` 重新注册 |
| relay 25s 内未确认配对 | 手机未接入时属正常（中继静默不响应） | 手机 App 打开后会自动配对，无需处理 |
| 中继密钥轮换后连不上 | RELAY_E2EE_KEY / RELAY_TOKEN 不一致 | 参照 OPS-002 §7 / OPS-003 §1.1 同步云 + 本地 .env 后 `restart relay` |

---

## 4. 计划任务卸载

```powershell
# 卸载 Jax-* 计划任务（AtStartup + Every5Min）
powershell -ExecutionPolicy Bypass -File scripts/install-scheduled-tasks.ps1 -Uninstall
```

验证：

```powershell
Get-ScheduledTask -TaskName "Jax-*"   # 应无结果
```

---

## 5. 验证记录（2026-08-05 实测）

- `jax-services.ps1 status`：model OK / backend OK / relay RUNNING(x1) ✅
- `start` 幂等：三件套已在运行全部跳过，exit 0 ✅
- `restart relay`：杀掉 .venv 启动器 + 子 python 整对，重新拉起单实例 ✅
- watchdog 实测（模拟掉线）：停 relay → 跑 watchdog → 日志 `[relay] 异常，拉起第 1 次 ...` + `[relay] 拉起成功（复查健康）`，relay 恢复 ✅
- watchdog 防风暴：连续 3 次拉起后第 4 次被拦截（`异常但 10 分钟内已重启 3 次，跳过拉起`），relay 保持停止（不空转） ✅
- 计划任务：Jax-Watchdog-AtStartup / Jax-Watchdog-Every5Min 注册成功，手动触发 LastTaskResult=0 ✅
- pytest：全量 **288 passed**（含新增 test_relay_client_fake_dead.py 5 例） ✅

## 6. 回滚

- 脚本回滚：`scripts/jax-services.ps1`、`scripts/jax-watchdog.ps1`、`scripts/install-scheduled-tasks.ps1` 均独立可删；删除后手动用原 `start-all.ps1` 启动。
- relay_client 回滚：去掉 `_relay_loop` 中 `wait_for(recv, timeout=20)` 与 `_record_pair_timeout/_wait_relay_backoff` 三处调用即恢复旧行为（重连退避不变）。
- 计划任务回滚：见 §4 卸载。
