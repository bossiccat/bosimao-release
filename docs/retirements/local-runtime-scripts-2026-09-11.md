# 本地运行态脚本退役记录

日期：2026-09-11 ｜ 项目：贾克斯·星核 / jax-pet
执行依据：用户政策 —— **不得用脚本解决问题；不得走本地；产品必须完全走云端。**

---

## 1. 为什么退役

这些脚本存在的唯一理由是「PC 上有一份需要被守护的本地运行态」：
本机跑 `jax-model(:19080)` / `jax-backend(:8000)` / `relay_client` / `rtc_bridge` / `sidecar`，
再由 watchdog + 计划任务每 5 分钟自愈一次。

2026-09-11 云端对端 `jax-voice-bridge` 已在 CloudRun 常驻运行并经产品自身端点验证：

```
GET /api/v1/voice/bridge/status → HTTP 200 | ok=true
rtc_bridge : {alive:true, pid 7, starts 1, exit_code:null}
sidecar    : {alive:true, pid 9, starts 1, exit_code:null}
rtc_bridge_health = ok  |  trtc_sdk_version = 13.4.802-beta.3
```

容器内 supervisor 的子进程退出即整体非零退出 → **由平台健康检查与重启替代本地 watchdog**。
本地守护的存在前提消失。

---

## 2. 退役清单（12 个运行时脚本 + 6 个专属契约测试）

| 文件 | 原职责 |
|---|---|
| `scripts/jax-watchdog.ps1` | 三件套自愈 watchdog（计划任务驱动） |
| `scripts/install-scheduled-tasks.ps1` | 注册 `Jax-Watchdog-AtStartup` / `Jax-Watchdog-Every5Min` 计划任务 |
| `scripts/watchdog-check.ps1` | 已废弃（DEPRECATED，仅打印警告） |
| `scripts/watchdog-sidecar.ps1` | 已废弃（DEPRECATED，仅打印警告） |
| `scripts/jax-services.ps1` | 本地三件套服务生命周期管理（35KB） |
| `scripts/start-all.ps1` | PC 一键启动入口（转调 jax-services） |
| `scripts/start-model.ps1` | 本地模型服务启动 |
| `scripts/start-relay.ps1` | 本地中继启动 |
| `scripts/lib-common.ps1` | 上述脚本共用库（健康探测/进程识别等） |
| `scripts/dev.ps1` | 一键开发启动（本地模型+后端+前端） |
| `scripts/adb-keepalive.bat` | adb 自动重连循环（本地自愈） |
| `scripts/adb-setup-phone.bat` | adb 无线调试设置 |
| `scripts/test/jax-watchdog-sidecar-ownership.test.js` | 守护 jax-watchdog（随脚本退役） |
| `scripts/test/proxy-env-fold-contract.test.js` | 守护 jax-services 启动点 |
| `scripts/test/relay-convergence-contract.test.js` | 守护 relay 收敛逻辑 |
| `scripts/test/relay-singleton-contract.test.js` | 守护 relay 单实例 |
| `scripts/test/rtc-bridge-stop-convergence-contract.test.js` | 守护 bridge 停止收敛 |
| `scripts/test/sidecar-credential-provision-orchestration.test.js` | 守护 sidecar 凭证编排（本地路径） |

**顺带作废的一处本地绕行**：`scripts/jax-services.ps1` 有一处未提交改动，把 backend 从
`--host 127.0.0.1` 改为 `--host 0.0.0.0`，目的是让手机经 **Tailscale**（`100.88.27.52:8000`）
直连替代 `adb reverse`。文件退役后该绕行随之消失。

---

## 3. 保留项（属构建/测试/诊断，不是"用脚本解决产品问题"）

`device-acceptance-capture.ps1`（真机取证）、`download_model.ps1` / `download_sherpa_models.py`（模型下载）、
`setup_env.ps1`（环境初始化）、`check-ui-p0.py`、`e2e_verify.py`、`mock_phone_client.py`、
`cleanup-*.py`（一次性磁盘运维）、`scripts/lib/sidecar-*.js`（sidecar 打包/运行时工具链，有独立测试）、
`scripts/test_release_governance/`（发布门禁测试，CI 会跑）。

---

## 4. 机器侧残留（需单独处置）

脚本从仓库退役 **不等于** 机器上的计划任务消失。本机仍可能注册着：

- `Jax-Watchdog-AtStartup`
- `Jax-Watchdog-Every5Min`

它们指向的 `jax-watchdog.ps1` 已不在仓库中；任务继续运行只会不断失败或拉起已退役的本地栈。
**卸载命令（可逆，随时可重新注册）**：

```powershell
Unregister-ScheduledTask -TaskName Jax-Watchdog-AtStartup -Confirm:$false
Unregister-ScheduledTask -TaskName Jax-Watchdog-Every5Min -Confirm:$false
```

（原 `install-scheduled-tasks.ps1 -Uninstall` 亦可，但该脚本已随本次退役删除，故直接给 cmdlet。）

---

## 5. 如何回滚

全部为受跟踪文件的删除，`git revert <本提交>` 或逐文件 `git checkout <本提交>^ -- <路径>` 即可完整还原。
计划任务按第 4 节的注册脚本原样重建（历史版本可 `git show <本提交>^:scripts/install-scheduled-tasks.ps1`）。
