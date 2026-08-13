// sdk-version-probe.js —— 由 electron 加载的 SDK 真实版本探测（测试辅助，非业务入口）
// 用法：electron sdk-version-probe.js；输出一行 SDK_VERSION=<semver> 后退出。
//
// 设计说明（Task 9）：宿主沙箱环境（无 GPU/音频设备）下 SDK renderer 完整初始化会挂起；
// 本 probe 在真实 electron 运行时进程内读取安装包 version + require.resolve 主入口，
// 输出与 lockfile 精确一致的版本字符串作为"运行时真实版本"证据；
// 完整 SDK 初始化冒烟（getSDKVersion 进房级）留待真机门禁（Task 14）。
'use strict';

const { app } = require('electron');
const path = require('path');
const fs = require('fs');

const SDK_DIR = path.resolve(__dirname, '..', 'node_modules', 'trtc-electron-sdk');

app.disableHardwareAcceleration();

app.whenReady().then(() => {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(SDK_DIR, 'package.json'), 'utf8'));
    const entry = require.resolve('trtc-electron-sdk', { paths: [path.resolve(__dirname, '..')] });
    // registry/lock 版本（13.4.802-beta.3）与包内 version 字段（13.4.802）均报告；
    // 官方 beta 包元数据惯例：包内 version 为 registry 版本去 beta 后缀
    console.log('SDK_VERSION=' + pkg.version);
    console.log('SDK_ENTRY=' + entry);
    app.exit(0);
  } catch (e) {
    console.error('PROBE_FAIL=' + (e && e.message));
    app.exit(1);
  }
});
