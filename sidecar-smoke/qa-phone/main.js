// qa-phone 主进程（L1 测试用，运行时生成）：加载渲染进程注入 WAV
const { app, BrowserWindow } = require('electron');
const path = require('path');
app.whenReady().then(() => {
  const win = new BrowserWindow({
    show: false, width: 320, height: 240,
    webPreferences: { nodeIntegration: true, contextIsolation: false },
  });
  const args = process.argv.slice(1).filter((a) => a.startsWith('--')).join('&');
  win.loadFile(path.join(__dirname, 'index.html'), { query: { args } });
});
