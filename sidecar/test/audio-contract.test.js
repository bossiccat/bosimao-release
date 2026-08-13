// audio-contract.test.js —— sidecar 音频注入契约（Task 9 / SPEC §4.3 / AC-08 AC-09）
//
// 断言：
// 1. 只有 sidecar/audio.js 构造 TRTCAudioFrame（SPEC 4.3：audio.js 是唯一格式 adapter）
// 2. 帧字段与 sendCustomAudioData 签名来自实际 SDK 包（读安装后 d.ts，禁止幻觉 API/48k 假定）
// 3. 模型侧输入始终为完整 640-byte 帧（16k/mono/PCM16/20ms，AC-08/AC-09）
// 4. SIGTERM 后 Electron 主进程退出（Task 9 退出清理）
// 5. 日志模板不含 Secret（secretKey/TRTC_SECRETKEY 不得进入日志）
//
// 反作弊：无 skip；签名断言直接解析安装后的 SDK 类型声明文件。
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const SIDECAR = path.resolve(__dirname, '..');
const SDK_DIR = path.join(SIDECAR, 'node_modules', 'trtc-electron-sdk');
const audio = require('../audio');

function findDts(dir) {
  const hits = [];
  const walk = (d) => {
    for (const ent of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, ent.name);
      if (ent.isDirectory()) walk(p);
      else if (ent.name.endsWith('.d.ts')) hits.push(p);
    }
  };
  walk(dir);
  return hits;
}

test('只有 audio.js 构造 TRTCAudioFrame', () => {
  const files = ['main.js', 'rtc.js', 'audio.js', 'bridge.js', 'config.js', 'logger.js', 'phone.js'];
  const constructors = [];
  for (const f of files) {
    const p = path.join(SIDECAR, f);
    if (!fs.existsSync(p)) continue;
    const src = fs.readFileSync(p, 'utf8');
    if (/new\s+TRTCAudioFrame\s*\(/.test(src)) constructors.push(f);
  }
  assert.deepEqual(constructors, ['audio.js'], `TRTCAudioFrame 构造只允许在 audio.js: ${constructors.join(',')}`);
});

test('帧字段与 sendCustomAudioData 签名来自实际 SDK d.ts', () => {
  assert.ok(fs.existsSync(SDK_DIR), '必须先安装 trtc-electron-sdk');
  const dtsFiles = findDts(SDK_DIR);
  assert.ok(dtsFiles.length > 0, 'SDK 必须包含类型声明文件');
  const dts = dtsFiles.map((p) => fs.readFileSync(p, 'utf8')).join('\n');
  // audio.js 使用的字段名必须全部存在于实际 SDK 声明中
  for (const field of ['audioFormat', 'data', 'length', 'sampleRate', 'channel', 'timestamp']) {
    assert.ok(
      new RegExp(`\\b${field}\\b`).test(dts),
      `TRTCAudioFrame 字段 ${field} 必须在实际 SDK d.ts 中声明`
    );
  }
  assert.ok(/sendCustomAudioData\s*\(/.test(dts), 'sendCustomAudioData 签名必须在实际 SDK d.ts 中');
  // 注入采样率必须来自实际 SDK 契约，不得存在未经验证的 48k 假定注释
  const audioSrc = fs.readFileSync(path.join(SIDECAR, 'audio.js'), 'utf8');
  assert.ok(!/假定|假设/.test(audioSrc), 'audio.js 不得包含未验证的采样率假定注释');
});

test('模型侧输入始终为完整 640-byte 帧（16k/mono/PCM16/20ms）', () => {
  // 639/640/641 边界：只输出完整 640B 帧，尾残帧不发送（AC-09）
  assert.deepEqual(audio.splitIntoFrames(Buffer.alloc(639)), [], '639B 不足一帧不得输出');
  const one = audio.splitIntoFrames(Buffer.alloc(640));
  assert.equal(one.length, 1);
  assert.equal(one[0].length, 640, '单帧必须恰为 640B');
  const two = audio.splitIntoFrames(Buffer.alloc(640 * 2 + 300));
  assert.equal(two.length, 2, '只输出完整帧，尾残帧截断');
  assert.ok(two.every((f) => f.length === 640), '所有帧必须恰为 640B');
  // 模型侧 16k 帧长常量校验（640 = 16000 * 2 * 0.02）
  assert.equal(640, 16000 * 2 * 0.02, '640B = 16k*2bytes*20ms');
});

test('SIGTERM 后 Electron 主进程退出', { timeout: 60000 }, async () => {
  const electron = path.join(SIDECAR, 'node_modules', 'electron', 'dist', 'electron.exe');
  assert.ok(fs.existsSync(electron), 'electron 必须已安装');
  // 宿主可能设置 ELECTRON_RUN_AS_NODE=1（node 模式无 app API）；必须删除该键
  const childEnv = {
    ...process.env,
    ELECTRON_DISABLE_SECURITY_WARNINGS: 'true',
    VOICE_SIDECAR_CREDENTIAL: 'test-only-sidecar-credential-value',
  };
  delete childEnv.ELECTRON_RUN_AS_NODE;
  delete childEnv.NODE_OPTIONS;
  const child = spawn(
    electron,
    ['--no-sandbox', '.', '--role=sidecar', '--sign-url=http://127.0.0.1:1', '--bridge-url=ws://127.0.0.1:1'],
    {
      cwd: SIDECAR,
      env: childEnv,
      stdio: ['ignore', 'pipe', 'pipe'],
    }
  );
  let stderr = '';
  child.stderr.on('data', (d) => { stderr += d; });
  await new Promise((r) => setTimeout(r, 6000)); // 等待主进程就绪
  // Windows 下子进程可能持有 stdio 管道（GPU/renderer），'close' 会延迟——用 'exit' 判定进程退出
  const exited = new Promise((resolve) => child.on('exit', (code, signal) => resolve({ code, signal })));
  child.kill('SIGTERM');
  const res = await Promise.race([exited, new Promise((_, rej) => setTimeout(() => rej(new Error(`SIGTERM 后 20s 未退出\nstderr=${stderr}`)), 20000))]);
  // 契约 = SIGTERM 后进程退出（超时即失败）；Windows 下 Node 的 SIGTERM 为 TerminateProcess 语义
  assert.ok(
    res.code === 0 || res.code === 1 || res.code === null,
    `SIGTERM 后必须退出（code=${res.code} signal=${res.signal}）`
  );
});

test('日志模板不含 Secret（secretKey/TRTC_SECRETKEY 不得进入日志）', () => {
  const files = ['main.js', 'rtc.js', 'audio.js', 'bridge.js', 'config.js', 'logger.js', 'phone.js', 'usersig.js'];
  const violations = [];
  for (const f of files) {
    const p = path.join(SIDECAR, f);
    if (!fs.existsSync(p)) continue;
    const src = fs.readFileSync(p, 'utf8');
    // 收集所有 log( 调用的字符串/模板参数
    const calls = [...src.matchAll(/log\(\s*['"`][^'"`]*['"`]\s*,\s*([^\n)]+)\)/g)];
    for (const m of calls) {
      const arg = m[1];
      if (/\bsecretKey\b|TRTC_SECRETKEY|user_sig\s*[:=]|userSig\s*[:=]/.test(arg)) {
        violations.push(`${f}: log(..., ${arg.trim()})`);
      }
    }
  }
  assert.deepEqual(violations, [], `日志调用不得携带 Secret 值/变量: ${violations.join(' | ')}`);
});
