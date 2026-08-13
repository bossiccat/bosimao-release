'use strict';

const { app, BrowserWindow, ipcMain } = require('electron');
const { EXIT_CHANNEL, createExitArbiter } = require('../exit-protocol');

const kind = process.argv.at(-1);
const arbiter = createExitArbiter((code) => app.exit(code));
ipcMain.on(EXIT_CHANNEL, (_event, payload) => arbiter.decide(payload));

app.whenReady().then(() => {
  const win = new BrowserWindow({
    show: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false },
  });
  const html = `<script>require('electron').ipcRenderer.send('${EXIT_CHANNEL}', {kind:'${kind}'})<\/script>`;
  win.loadURL(`data:text/html,${encodeURIComponent(html)}`);
});
