// adev-device-inventory.test.js —— 锁定「本端设备表」观测的行为契约。
//
// 为什么需要这个文件
// ------------------
// 2026-09-16 容器事故：手机在正常发布（`RtcCustomAudio: lvl raw=64 … gate=true`），
// sidecar 却连续 70s `[STAT] up=0帧`、`[PCM]`/`[UPRMS]` 一行都没有。真因是**本端没有
// 可用播放设备**（TRTC 原生层 `Player error player device list is empty` / code 1202 /
// `kPlayout produced 0 ms data, callback count is 0` / jitter buffer `io_last_read_frame_ticks: -1`），
// 而这条因果链在我们自己的日志里**一个字都没有** —— 于是「为什么 up=0」成了黑盒。
//
// `sidecar/adev.js` 就是那层翻译：直接问 SDK 要设备表，并在 1202 到来时把后果写清楚。
// 本文件守住三件事，每一件坏了都会让下一次同类事故重新变成黑盒：
//   1) 清点**绝不抛**（观测代码不得成为新的故障面）：方法缺失/抛错/返回非数组都要降级；
//   2) 设备表为空时必须显式点出「⇒ 上行恒为 0」，而不是留一个 speakers=0 让人自己推；
//   3) 只有 1202 才归类为音频设备问题（其余警告不得冒充，否则 `[ADEV]` 这个作用域就失去判别力）。
//
// 运行：node --test test/adev-device-inventory.test.js（在 sidecar/ 下）
// 硬约束：本文件**零外部依赖**，只 require 本仓 sidecar/ 下的纯 JS。
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const adev = require(path.join(__dirname, '..', 'adev.js'));

/**
 * 造一个只有两个设备表方法的替身（getSpeakerDevicesList/getMicDevicesList）。
 * `speakers`/`mics` 是**方法的返回值**；`throws` 非空时方法改为抛错（模拟原生层异常）。
 * 不传（undefined）时**不挂该方法**，用于验证「方法缺失」这条降级路径。
 */
function makeCloud({ speakers, mics, throws } = {}) {
  const cloud = {};
  if (speakers !== undefined) {
    cloud.getSpeakerDevicesList = () => {
      if (throws) throw new Error(throws);
      return speakers;
    };
  }
  if (mics !== undefined) {
    cloud.getMicDevicesList = () => {
      if (throws) throw new Error(throws);
      return mics;
    };
  }
  return cloud;
}

/** 真实形状：SDK 返回 TRTCDeviceInfo[]，字段是 deviceName/deviceId。 */
const SPEAKER = { deviceName: 'JaxNullSink', deviceId: 'jax_null' };
const MIC = { deviceName: 'JaxNullMic', deviceId: 'jax_null.mic' };

// --- 1. 清点绝不抛 ----------------------------------------------------------

test('inventory 在方法缺失时不抛，并把原因带出来', () => {
  const inv = adev.inventory({});
  assert.deepStrictEqual(inv.speakers, []);
  assert.deepStrictEqual(inv.mics, []);
  assert.match(inv.speakersError, /getSpeakerDevicesList/);
  assert.match(inv.micsError, /getMicDevicesList/);
});

test('inventory 在方法抛错时不抛，把异常文本降级成 error', () => {
  const inv = adev.inventory(makeCloud({ speakers: [], mics: [], throws: 'native addon not ready' }));
  assert.deepStrictEqual(inv.speakers, []);
  assert.strictEqual(inv.speakersError, 'native addon not ready');
});

test('inventory 在返回非数组时判为异常而不是当成 0 台', () => {
  const inv = adev.inventory(makeCloud({ speakers: null, mics: undefined }));
  assert.deepStrictEqual(inv.speakers, []);
  assert.match(inv.speakersError, /非数组/);
});

test('inventory 对 cloud 为 null 也不抛', () => {
  assert.doesNotThrow(() => adev.inventory(null));
});

// --- 2. 空设备表必须自带结论 ------------------------------------------------

test('formatInventory 在播放设备为空时显式点出「上行恒为 0」', () => {
  const text = adev.formatInventory({ speakers: [], mics: [MIC.deviceName], speakersError: '', micsError: '' });
  assert.match(text, /speakers=0\(空\)/);
  assert.match(text, /mics=1\(JaxNullMic\)/);
  assert.match(text, /上行恒为 0/, '空设备表必须自带后果，否则又变成「看到 0 自己猜」');
});

test('formatInventory 在设备齐全时不带那句后果（避免噪声掩盖异常）', () => {
  const text = adev.formatInventory({
    speakers: [SPEAKER.deviceName], mics: [MIC.deviceName], speakersError: '', micsError: '',
  });
  assert.match(text, /speakers=1\(JaxNullSink\)/);
  assert.doesNotMatch(text, /上行恒为 0/);
});

test('formatInventory 把清点异常一并带出来（否则退化成「看起来是 0 台」）', () => {
  const text = adev.formatInventory({ speakers: [], mics: [], speakersError: 'boom', micsError: '' });
  assert.match(text, /清点异常=boom/);
});

test('设备名缺失时退化为 deviceId，不产生空标签', () => {
  const inv = adev.inventory(makeCloud({
    speakers: [{ deviceId: 'id-only' }], mics: [{}],
  }));
  assert.deepStrictEqual(inv.speakers, ['id-only']);
  assert.deepStrictEqual(inv.mics, ['<unnamed>']);
});

// --- 3. 只有 1202 才是音频设备问题 ------------------------------------------

test('1202 归为音频设备问题，且带上当下设备表与后果', () => {
  const line = adev.onWarningLine(adev.PLAYER_DEVICE_EMPTY_CODE, 'player device list is empty',
    { speakers: [], mics: [MIC.deviceName], speakersError: '', micsError: '' });
  assert.strictEqual(line.audioDevice, true);
  assert.match(line.text, /errCode=1202/);
  assert.match(line.text, /player device list is empty/);
  assert.match(line.text, /speakers=0\(空\)/);
  assert.match(line.text, /上行必为 0/);
});

test('1202 没有设备表时也照样输出（不得因为缺少计数而不报）', () => {
  const line = adev.onWarningLine(adev.PLAYER_DEVICE_EMPTY_CODE, 'player device list is empty', null);
  assert.strictEqual(line.audioDevice, true);
  assert.match(line.text, /errCode=1202/);
  assert.doesNotMatch(line.text, /speakers=/);
});

test('其它警告码不得冒充音频设备问题', () => {
  const line = adev.onWarningLine(1201, 'some other warning', { speakers: [], mics: [], speakersError: '', micsError: '' });
  assert.strictEqual(line.audioDevice, false);
  assert.match(line.text, /errCode=1201/);
  assert.doesNotMatch(line.text, /上行必为 0/, '非 1202 的警告不得借用 1202 的结论');
});

test('警告码是字符串型数字时仍判为 1202（SDK 回调可能是字符串）', () => {
  assert.strictEqual(adev.onWarningLine('1202', 'x', null).audioDevice, true);
});

test('警告信息缺失时不抛（回调参数可能为空）', () => {
  const line = adev.onWarningLine(adev.PLAYER_DEVICE_EMPTY_CODE, undefined, null);
  assert.match(line.text, /errCode=1202/);
});
