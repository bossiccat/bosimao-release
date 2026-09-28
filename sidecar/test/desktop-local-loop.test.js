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
  assert.equal(validateStartup(args, {}), 'DESKTOP_DEVICE_CREDENTIAL_MISSING');
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
  assert.doesNotMatch(codeOnly, /sendUpAudio/);
  assert.doesNotMatch(codeOnly, /sendCustomAudioData/);
  assert.doesNotMatch(codeOnly, /session\/pending/);
  assert.doesNotMatch(codeOnly, /new BridgeClient/);
  assert.doesNotMatch(codeOnly, /127\.0\.0\.1:19092/);
});
