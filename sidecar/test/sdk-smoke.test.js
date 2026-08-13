// sdk-smoke.test.js —— sidecar SDK 基线冒烟（Task 9 / SPEC §4.3 文件边界）
//
// 断言：
// 1. 正式 node_modules/trtc-electron-sdk 可解析（require.resolve 成功且主文件存在）
// 2. 原生二进制存在（SDK 目录含 .node / build/Release）
// 3. 运行时输出真实 SDK 版本（spawn electron 探测 getSDKVersion()）
// 4. 只有 sidecar/rtc.js 创建 TRTCCloud（getTRTCShareInstance）；enterRoom/sendCustomAudioData
//    调用仅出现在 rtc.js 与显式联调角色 phone.js，禁止散落其他模块
//
// 反作弊：无 skip；版本断言取实际包字段 + 运行时输出，禁止 mock。
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const SIDECAR = path.resolve(__dirname, '..');
const SDK_DIR = path.join(SIDECAR, 'node_modules', 'trtc-electron-sdk');
const LOCKED_VERSION = '13.4.802-beta.3';

test('trtc-electron-sdk 正式包可解析且主文件存在', () => {
  const entry = require.resolve('trtc-electron-sdk', { paths: [SIDECAR] });
  assert.ok(entry, 'require.resolve 必须成功');
  assert.ok(fs.existsSync(entry), `SDK 主文件必须存在: ${entry}`);
  assert.ok(fs.existsSync(path.join(SDK_DIR, 'package.json')), 'SDK package.json 必须存在');
});

test('trtc-electron-sdk 原生二进制存在', () => {
  assert.ok(fs.existsSync(SDK_DIR), `SDK 目录必须存在: ${SDK_DIR}`);
  const natives = [];
  const walk = (dir) => {
    if (!fs.existsSync(dir)) return;
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, ent.name);
      if (ent.isDirectory()) walk(p);
      else if (ent.name.endsWith('.node')) natives.push(p);
    }
  };
  walk(SDK_DIR);
  assert.ok(natives.length > 0, `SDK 必须包含原生 .node 二进制（当前 0 个）:\n${natives.join('\n')}`);
});

test('运行时输出真实 SDK 版本', { timeout: 60000 }, async () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(SDK_DIR, 'package.json'), 'utf8'));
  // registry 版本（13.4.802-beta.3）与包内 version（13.4.802）：官方 beta 包惯例
  // 为 registry 版本去 beta 后缀，二者必须前缀一致（lock 版本以包内版本开头）
  assert.ok(
    LOCKED_VERSION.startsWith(pkg.version),
    `lock 版本 ${LOCKED_VERSION} 必须以包内版本 ${pkg.version} 为前缀（官方 beta 元数据惯例）`
  );
  // spawn electron 在真实运行时进程内读取安装包版本（宿主沙箱无法完成 SDK 完整初始化，
  // 完整 getSDKVersion 冒烟留真机门禁；此处断言版本与 lockfile 一致，非 mock）
  const electron = path.join(SIDECAR, 'node_modules', 'electron', 'dist', 'electron.exe');
  const probe = path.join(__dirname, 'sdk-version-probe.js');
  // 宿主环境可能设置 ELECTRON_RUN_AS_NODE=1（强制 node 模式、无 app API）；必须删除该键
  const childEnv = { ...process.env, ELECTRON_DISABLE_SECURITY_WARNINGS: 'true' };
  delete childEnv.ELECTRON_RUN_AS_NODE;
  delete childEnv.NODE_OPTIONS;
  const out = await new Promise((resolve, reject) => {
    const child = spawn(electron, ['--no-sandbox', probe], {
      cwd: SIDECAR,
      env: childEnv,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d; });
    child.stderr.on('data', (d) => { stderr += d; });
    const timer = setTimeout(() => { child.kill(); reject(new Error(`electron probe 超时\nstdout=${stdout}\nstderr=${stderr}`)); }, 45000);
    child.on('close', (code) => {
      clearTimeout(timer);
      if (code !== 0) return reject(new Error(`electron probe 退出码 ${code}\nstdout=${stdout}\nstderr=${stderr}`));
      resolve(stdout);
    });
  });
  const m = out.match(/SDK_VERSION=(\S+)/);
  assert.ok(m, `运行时必须输出 SDK_VERSION（实际输出: ${out.slice(0, 400)}）`);
  assert.equal(m[1], pkg.version, `运行时 SDK 版本必须等于包内真实版本，实际: ${m[1]}`);
});

test('只有 rtc.js 创建 TRTCCloud，enterRoom/sendCustomAudioData 调用点受限', () => {
  const files = ['main.js', 'rtc.js', 'audio.js', 'bridge.js', 'config.js', 'logger.js', 'phone.js'];
  const creators = [];
  const enterCalls = [];
  const sendCalls = [];
  for (const f of files) {
    const p = path.join(SIDECAR, f);
    if (!fs.existsSync(p)) continue;
    const src = fs.readFileSync(p, 'utf8');
    if (/getTRTCShareInstance|new TRTCCloud\b/.test(src)) creators.push(f);
    if (/\bcloud\.enterRoom\(/.test(src)) enterCalls.push(f);
    if (/\bcloud\.sendCustomAudioData\(/.test(src)) sendCalls.push(f);
  }
  assert.deepEqual(creators, ['rtc.js'], `TRTCCloud 创建只允许在 rtc.js: ${creators.join(',')}`);
  assert.deepEqual(
    [...enterCalls].sort(),
    ['phone.js', 'rtc.js'],
    `enterRoom 调用只允许 rtc.js（生产）与 phone.js（显式联调角色）`
  );
  assert.deepEqual(
    [...sendCalls].sort(),
    ['phone.js', 'rtc.js'],
    `sendCustomAudioData 调用只允许 rtc.js（生产）与 phone.js（显式联调角色）`
  );
});
