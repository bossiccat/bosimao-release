'use strict';

// shutdown 文件信号轮询（2026-10-05 Electron 43 stdin 双回归迁移，见 main.js）。
// 父进程 spawn 时生成唯一路径经 JAX_SIDECAR_SHUTDOWN_FILE 传入，停机时创建该
// 文件；本模块轮询其出现并回调受控退出。独立成模块以便 node:test 单测覆盖。

const fs = require('fs');

// 轮询而非 fs.watch：后者的事件可靠性跨平台差异大（Windows 桌面 + Linux
// CloudRun 容器两端都要跑），存在性轮询是唯一在两端行为一致的观测方式。
function watchShutdownFile(filePath, onShutdown, { intervalMs = 500 } = {}) {
  if (!filePath) return null; // env 未设 = 不启用（开发态直跑兼容）
  const timer = setInterval(() => {
    try {
      if (fs.existsSync(filePath)) {
        clearInterval(timer); // 文件一经检测即停止轮询（退出语义一次性）
        onShutdown();
      }
    } catch (_) {
      // 单次探测失败（如临时目录权限抖动）：继续轮询；父进程侧 stop()
      // 的超时强杀兜底。
    }
  }, intervalMs);
  return timer;
}

module.exports = { watchShutdownFile };
