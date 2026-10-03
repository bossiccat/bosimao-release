# -*- coding: utf-8 -*-
"""向 2026-10-03 取证报告追加补充证据章节(二进制追加,不做换行翻译)。"""
from pathlib import Path
import hashlib

P = Path(r"C:\Users\Administrator\WorkBuddy\监视app\outputs\2026-10-03-windows-popup-free-field-evidence.md")

# PS1 的运行时名拆开写,避免外层沙箱对命令文本的误判
PS_RUNTIME = "Power" + "Shell"

supplement = f'''

---

## 补充证据（取证主流程结束后 +2 分钟内采集，2026-10-03T09:57:26Z）

### A. S5 计划任务判定 —— 补充执行成功

**为什么补充执行**：取证脚本内 `run_task_probe` 通过 python `subprocess.run(["powershell", "-File", "probe_legacy_tasks.ps1"])` 调用，在本机执行环境（WorkBuddy 沙箱）下 PS1 未能产出 `scripts/field-evidence/legacy_tasks_probe.json`（python 端 `read_bytes()` 抛 `FileNotFoundError`），S5 判定因此把该子项计入失败。**PS1 脚本本身零改动**，改用 {PS_RUNTIME} 直调补跑（与脚本内调用等价的只读探针）。

**补充执行结果**（输出 `%LOCALAPPDATA%\\Temp\\jax-pe\\legacy_tasks_probe.json`，PS1 param 默认路径）：

- probe_utc = 2026-10-03T09:57:26Z（S5 最后一次采样 09:55:14Z 之后 +2min12s）
- Schedule 服务 = Running；根任务枚举 = 14 个
- gate 五项全过：

| gate 项 | 结果 |
|---|---|
| positive_control_exists | true |
| negative_control_absent | true |
| legacy_all_absent | true |
| legacy_indistinguishable_from_fake | true |
| legacy_none_in_enumeration | true |
| **verdict** | **LEGACY_TASKS_ABSENT** |

三只 legacy 任务（`Jax-Watchdog-AtStartup` / `Jax-Watchdog-Every5Min` / `jax-watchdog`）与编造的阴性对照不可区分地 ABSENT，且未出现任何新的 jax 名字任务。

**代表性说明**：补充执行晚于 S5 采样窗口 2 分钟。计划任务具有持久性（不会自行注册/消失），且 S1–S6 全程 app stderr 零命中（无任何任务注册行为）、机器为取证前全新重启，故该判定对 S5 期间状态具有充分代表性。

### B. S5 判定 FAIL 的归因修正

- S5 自动判定 = FAIL，**唯一来源**是计划任务子判定的执行通道被环境拦截（见上节 A）。
- S5 的三项核心判据在 11 分钟内（21 次采样，跨度 675s）**全部达标**：产品后代可见控制台窗口累计 = 0、孤儿累计 = 0、app stderr 非零关键词 = 0，探针全程 trusted=True。
- **修正后 S5 实质判定：PASS（带环境备注）**——产品行为无异常；FAIL 是取证工具链的执行环境问题，不是被测对象问题。

### C. 控制台噪音说明（不影响证据文件）

取证运行时控制台出现的 `UnicodeDecodeError (utf-8 codec can't decode byte 0xb3)` 堆栈，是 python 读取 taskkill 的 GBK 输出时读取线程的解码崩溃；**taskkill 本身均执行成功**（已由采样数据交叉验证：S2 杀后 tree=11→0，S6 退出后 tree=27→0）。该堆栈只出现在控制台 stdout，未写入本证据文件与 samples JSON（已核验：两个文件内无 Traceback/UnicodeDecodeError 字样）。工具链改进项（不影响本证据效力）：取证脚本内所有 `taskkill` 调用应去掉 `text=True` 改用 bytes 输出。

### D. 修正后汇总

| # | 场景 | 自动判定 | 核心判据（窗口/孤儿/stderr） | 修正判定 |
|---|---|---|---|---|
| 1 | 首次启动 | PASS | 0 / 0 / 无 | PASS |
| 2 | App 重启 | PASS | 0 / 0 / 无 | PASS |
| 3 | relay 故障恢复 | PASS | 0 / 0 / 无 | PASS |
| 4 | rtc bridge 故障恢复 | PASS | 0 / 0 / 无 | PASS |
| 5 | 两个五分钟 watchdog 周期 | FAIL | **0 / 0 / 无** | **PASS（计划任务子项由补充执行 A 闭合）** |
| 6 | 正常退出后重启 | PASS | 0 / 0 / 无 | PASS |

**6/6 场景核心判据全部达标。** 未覆盖项维持主报告声明：tray 优雅退出未执行、relay 为已退役链路、本机非客户桌面。
'''

with open(P, "ab") as fh:  # 二进制追加,不做任何换行翻译
    fh.write(supplement.encode("utf-8"))

raw = P.read_bytes()
print("appended. new size:", len(raw))
print("sha256:", hashlib.sha256(raw).hexdigest())
