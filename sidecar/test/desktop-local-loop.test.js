'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { parseArgList, validateStartup } = require('../config');

const RTC_SOURCE = fs.readFileSync(path.join(__dirname, '..', 'rtc.js'), 'utf8');

function desktopArgs() {
  return parseArgList(['--role=desktop', '--device=pc-local-1']);
}

test('desktop role starts from its own device credential and never polls phone intents', () => {
  const args = desktopArgs();
  assert.equal(args.role, 'desktop');
  assert.equal(args.device, 'pc-local-1');
  assert.equal(
    validateStartup(args, { VOICE_DESKTOP_DEVICE_CREDENTIAL: 'pc-local-1.secret' }),
    null,
  );
  // 首次启动只有管理员凭证。设备凭证由 desktop-loop 现场注册，启动门不能先把它判死。
  assert.equal(
    validateStartup(args, { VOICE_OWNER_CREDENTIAL: 'owner-secret' }),
    null,
  );
  assert.equal(validateStartup(args, {}), 'DESKTOP_DEVICE_CREDENTIAL_MISSING');
  assert.match(RTC_SOURCE, /role === 'desktop'[\s\S]{0,600}VOICE_OWNER_CREDENTIAL/);
  assert.equal(
    validateStartup(parseArgList(['--role=desktop']), {}),
    'DESKTOP_DEVICE_REQUIRED',
  );
  assert.match(RTC_SOURCE, /VOICE_DESKTOP_DEVICE_CREDENTIAL/);
  assert.match(RTC_SOURCE, /runDesktop\(cloud,\s*log\)/);
  assert.doesNotMatch(RTC_SOURCE, /role === 'desktop'[\s\S]{0,400}pollAndJoin/);
});

test('desktop loop publishes the local mic and lets the room play the reply', () => {
  const loop = fs.readFileSync(path.join(__dirname, '..', 'desktop-loop.js'), 'utf8');
  const codeOnly = loop.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.match(codeOnly, /enableCustomAudioCapture\(false\)/);
  assert.match(codeOnly, /startLocalAudio\(/);
  assert.match(codeOnly, /entry_point:\s*'main'/);
  assert.match(codeOnly, /cloud\.enterRoom\(/);
  assert.match(codeOnly, /device_id:\s*ARGS\.device/);
  assert.match(codeOnly, /platform:\s*'windows'/);
  assert.match(codeOnly, /VOICE_OWNER_CREDENTIAL/);
  // 播放走房间。打断不能在这里抄一份麦克风：桌面自己签的会话没有可兑付的
  // hello，桥没有 hello 就不发帧，云端会拒绝无凭证的上行。喇叭音量打成 0
  // 只会让用户听不到回复。
  assert.doesNotMatch(codeOnly, /sendUpAudio/);
  assert.doesNotMatch(codeOnly, /setAudioPlayoutVolume\(0\)/);
  assert.doesNotMatch(codeOnly, /sendCustomAudioData/);
  assert.doesNotMatch(codeOnly, /session\/pending/);
  assert.doesNotMatch(codeOnly, /new BridgeClient/);
  assert.doesNotMatch(codeOnly, /127\.0\.0\.1:19092/);
});
