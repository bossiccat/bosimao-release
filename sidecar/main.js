// Electron 主进程：创建隐藏窗口并加载渲染进程脚本（TRTC SDK 需在渲染进程/DOM 环境运行）
// sidecar 无 UI：隐藏窗口常驻，职责最小化（进房 + 音频双向桥接 + 状态日志）
const { app, BrowserWindow } = require('electron');
const path = require('path');

app.whenReady().then(() => {
  const win = new BrowserWindow({
    show: false, // 隐藏窗口（无 UI sidecar）
    width: 320,
    height: 240,
    webPreferences: {
      nodeIntegration: true, // 渲染进程内 require('trtc-electron-sdk')
      contextIsolation: false,
    },
  });
  // 将命令行参数传给渲染进程（通过查询串）：--device / --role / --sign-url / --bridge-url / --wav / --hold
  const args = process.argv.slice(1).filter((a) => a.startsWith('--')).join('&');
  win.loadFile(path.join(__dirname, 'index.html'), { query: { args } });
});

// 全部窗口关闭后不退出（sidecar 常驻，由 rtc_bridge / 看门狗控制生命周期）
app.on('window-all-closed', (e) => {
  /* 保持进程存活，等待外部拉起/退出信号 */
});
