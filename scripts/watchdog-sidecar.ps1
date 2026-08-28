# ============================================================
# watchdog-sidecar.ps1 —— 【已废弃 DEPRECATED】sidecar 看门狗
# 废弃日期：2026-08-21（审计 A4 修复）
# 废弃原因：
#   1. 本脚本拉起 sidecar 时传 --device=sidecar-dev-1，而 sidecar/main.js 对
#      role=sidecar 禁止 --device 参数 → 启动即 fatal SIDECAR_UNEXPECTED_DEVICE_ARG
#      → 无限拉起无限死循环。
#   2. 健康判定用 sidecar_connected（空闲态本就是 false）→ 每 30s 误杀健康空闲 sidecar。
#   3. 系统已有计划任务 Jax-Watchdog-Every5Min / Jax-Watchdog-AtStartup 统一运行
#      jax-watchdog.ps1（含 sidecar 拉起，参数正确无 --device）——双看门狗同时管
#      sidecar 会互相冲突。
# 替代方案：scripts/jax-watchdog.ps1（单一看门狗，由 install-scheduled-tasks.ps1 注册）
# 本文件保留仅作考古，直接退出，不执行任何拉起/杀进程动作。
# ============================================================
$ErrorActionPreference = 'SilentlyContinue'
Write-Warning "[deprecated] watchdog-sidecar.ps1 已废弃（审计 A4）：sidecar 看门狗已收敛到 scripts/jax-watchdog.ps1（计划任务 Jax-Watchdog-* 统一驱动）。本脚本不执行任何动作。"
exit 1
