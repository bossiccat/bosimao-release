// Electron 主进程：创建隐藏窗口并加载渲染进程脚本（TRTC SDK 需在渲染进程/DOM 环境运行）
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
  // 将命令行参数传给渲染进程（通过查询串）
  const args = process.argv.slice(1).filter((a) => a.startsWith('--')).join('&');
  win.loadFile(path.join(__dirname, 'index.html'), { query: { args } });
});
