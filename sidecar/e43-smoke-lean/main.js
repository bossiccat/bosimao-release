// Electron 43 主进程：隐藏窗口加载渲染进程（TRTC SDK 需在渲染进程/DOM 环境运行）。
// 从 sidecar-smoke/main.js 复制；本目录位于 sidecar/ 下，require('trtc-electron-sdk')
// 解析到生产 node_modules（13.4.802-beta.3）。
const { app, BrowserWindow } = require('electron');
const path = require('path');

app.whenReady().then(() => {
  const win = new BrowserWindow({
    show: false,
    width: 320,
    height: 240,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });
  const args = process.argv.slice(1).filter((a) => a.startsWith('--')).join('&');
  win.loadFile(path.join(__dirname, 'index.html'), { query: { args } });
});
