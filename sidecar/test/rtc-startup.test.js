// rtc-startup.test.js —— 锁定「初始化失败必须可归因」这一契约。
//
// 背景（2026-09-12 事故）：原 `catch (_)` 把异常整个吞掉，只留下
// `[FATAL] SIDECAR_INITIALIZATION_FAILED`，导致 runSidecar() 内任何异常都无法定位
// （main-diag.log / 业务日志 / stdout 全是这一行）。本测试守住两条：
//   1. 失败路径必须把**异常详情**交给 logFatal；
//   2. 详情格式稳定（类型 + 信息 + 栈帧），且不含换行（日志按行消费）。
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const { startPollingRuntime, describeError } = require(
  path.join(__dirname, '..', 'rtc-startup.js'));

test('runSidecar 抛异常时，logFatal 必须收到可归因的详情', () => {
  const seen = [];
  const started = startPollingRuntime({
    runSidecar: () => { throw new TypeError('pacer is not a function'); },
    pollAndJoin: () => {},
    scheduleInterval: () => {},
    scheduleTimeout: () => {},
    requestFatal: () => seen.push('fatal'),
    logFatal: (detail) => seen.push(detail),
  });
  assert.strictEqual(started, false);
  assert.deepStrictEqual(seen[0] && seen[0].includes('TypeError'), true,
    '详情必须带异常类型');
  assert.deepStrictEqual(seen[0].includes('pacer is not a function'), true,
    '详情必须带异常信息（旧实现把这段整个吞掉）');
  assert.strictEqual(seen[1], 'fatal');
});

test('成功路径不调用 logFatal，且按 2s / 300ms 调度轮询', () => {
  const calls = [];
  const started = startPollingRuntime({
    runSidecar: () => calls.push('run'),
    pollAndJoin: () => calls.push('poll'),
    scheduleInterval: (fn, ms) => calls.push(`interval:${ms}`),
    scheduleTimeout: (fn, ms) => calls.push(`timeout:${ms}`),
    requestFatal: () => calls.push('fatal'),
    logFatal: () => calls.push('log'),
  });
  assert.strictEqual(started, true);
  assert.deepStrictEqual(calls, ['run', 'interval:2000', 'timeout:300']);
});

test('describeError 只取单行，不含换行（日志按行消费）', () => {
  const err = new Error('boom');
  err.stack = 'Error: boom\n    at runSidecar (rtc.js:99:7)\n    at main (rtc.js:420:1)';
  const s = describeError(err);
  assert.strictEqual(s.includes('\n'), false);
  assert.deepStrictEqual(s.includes('err=Error: boom'), true);
  assert.deepStrictEqual(s.includes('rtc.js:99:7'), true, '应带上首个栈帧，便于定位');
});

test('describeError 对空值/非 Error 也不抛', () => {
  assert.strictEqual(describeError(null), 'err=<none>');
  assert.strictEqual(describeError(undefined), 'err=<none>');
  assert.deepStrictEqual(describeError('plain string').startsWith('err=Error: plain string'), true);
});
