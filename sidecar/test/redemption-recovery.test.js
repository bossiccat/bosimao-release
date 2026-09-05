'use strict';

// 兑付失败自愈契约（2026-09-05 真机证据驱动）：
//   backend 事件循环阻塞 111s → sign 往返挂死占住轮询 → hello proof（TTL=60s）
//   过期 → 兑付 40112 → sidecar 把 hello_redemption_failed 当致命错误退出
//   → 用户「再点立即监听没反应」。
// 契约：
//   C1 selectPendingIntent 支持第三参失败意图集合（跳过已死意图，防重签死循环）；
//   C2 失败意图跳过列表有界（防长驻进程内存无限增长）；
//   C3 兑付失败恢复状态机：连续失败 < 上限 → 可恢复；达到上限 → 应退出；成功重置；
//   C4 fetchJsonWithTimeout：成功路径返回解析结果并取消定时器；超时路径 abort；
//   C5 rtc.js 源码契约：hello_redemption_failed 走恢复路径而非 exitSidecar；
//      sign/pending 请求走带超时的 fetch。

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const {
  createIntentSkipList,
  createRecoveryState,
  fetchJsonWithTimeout,
} = require('../intent-recovery');
const { selectPendingIntent } = require('../intent-selection');

const RTC_SOURCE = fs.readFileSync(path.join(__dirname, '..', 'rtc.js'), 'utf8');

function intent(sessionId, roomId, deviceId = 'device-a') {
  return { device_id: deviceId, room_id: roomId, session_id: sessionId };
}

// ---------- C1 selectPendingIntent 跳过失败意图 ----------

test('selectPendingIntent skips intents whose session_id is in the failed set', () => {
  const intents = [intent('session-a', 'jax-a'), intent('session-b', 'jax-b')];
  const failed = new Set(['session-a']);
  assert.deepEqual(selectPendingIntent(intents, null, failed), intents[1]);
  // 全部失败 → 无可选
  const failedAll = new Set(['session-a', 'session-b']);
  assert.equal(selectPendingIntent(intents, null, failedAll), null);
  // 第三参缺省 → 兼容既有两参调用语义
  assert.deepEqual(selectPendingIntent(intents, null), intents[0]);
});

test('selectPendingIntent still skips current room before failed-set filtering', () => {
  const intents = [intent('session-a', 'jax-room'), intent('session-b', 'jax-b')];
  const failed = new Set(['session-b']);
  assert.equal(selectPendingIntent(intents, 'jax-room', failed), null);
});

// ---------- C2 失败意图跳过列表有界 ----------

test('intent skip list is bounded and keeps the most recent entries', () => {
  const list = createIntentSkipList({ capacity: 3 });
  list.add('s1'); list.add('s2'); list.add('s3');
  assert.equal(list.has('s1'), true);
  list.add('s4'); // s1 被挤出
  assert.equal(list.has('s1'), false);
  assert.equal(list.has('s2'), true);
  assert.equal(list.has('s4'), true);
  assert.equal(list.size(), 3);
  // 空/假值不进列表
  list.add(undefined); list.add(null); list.add('');
  assert.equal(list.size(), 3);
});

// ---------- C3 兑付失败恢复状态机 ----------

test('recovery state stays recoverable until the consecutive limit', () => {
  const state = createRecoveryState({ maxConsecutiveFailures: 3 });
  assert.equal(state.recordFailure(), true);   // 1
  assert.equal(state.recordFailure(), true);   // 2
  assert.equal(state.recordFailure(), false);  // 3 → 达上限，应退出
});

test('recovery state reset clears the consecutive counter', () => {
  const state = createRecoveryState({ maxConsecutiveFailures: 2 });
  assert.equal(state.recordFailure(), true);
  state.reset();
  assert.equal(state.recordFailure(), true);
  assert.equal(state.recordFailure(), false);
});

// ---------- C4 fetchJsonWithTimeout ----------

test('fetchJsonWithTimeout returns parsed json and cancels the timer on success', async () => {
  const cancelled = [];
  const timers = [];
  const seenSignals = [];
  const fakeFetch = async (url, opts) => {
    seenSignals.push(opts.signal);
    return { json: async () => ({ code: 0, url }) };
  };
  const result = await fetchJsonWithTimeout('https://x/sign', {}, {
    timeoutMs: 5000,
    scheduleTimeout: (fn, ms) => { timers.push(ms); return 42; },
    cancelTimeout: (id) => cancelled.push(id),
    fetchImpl: fakeFetch,
  });
  assert.equal(result.code, 0);
  assert.ok(seenSignals[0] instanceof AbortSignal);
  assert.deepEqual(timers, [5000]);
  assert.deepEqual(cancelled, [42]);
});

test('fetchJsonWithTimeout aborts when the response does not arrive in time', async () => {
  const controllerRef = { current: null };
  const fakeFetch = async (url, opts) => {
    controllerRef.current = opts.signal;
    // 模拟挂死：只有 abort 才 reject
    return new Promise((_, reject) => {
      opts.signal.addEventListener('abort', () => {
        const err = new Error('aborted');
        err.name = 'AbortError';
        reject(err);
      });
    });
  };
  await assert.rejects(
    fetchJsonWithTimeout('https://x/pending', {}, {
      timeoutMs: 10,
      scheduleTimeout: (fn) => { fn(); return 1; },
      cancelTimeout: () => {},
      fetchImpl: fakeFetch,
    }),
    (err) => err.name === 'AbortError',
  );
  assert.equal(controllerRef.current.aborted, true);
});

// ---------- C5 rtc.js 源码契约 ----------

test('rtc.js routes hello_redemption_failed to recovery instead of fatal exit', () => {
  assert.match(RTC_SOURCE, /reason === 'hello_redemption_failed'/);
  assert.match(RTC_SOURCE, /recoverFromRedemptionFailure\(\)/);
  // 恢复路径必须清空房间与会话键并继续轮询（不得直接 exitSidecar）
  assert.match(RTC_SOURCE, /function recoverFromRedemptionFailure\(\)[\s\S]*?currentRoom = null[\s\S]*?currentSessionId = null/);
});

test('rtc.js caps consecutive redemption failures with a fatal exit guard', () => {
  assert.match(RTC_SOURCE, /hello_redemption_failed_exhausted/);
});

test('rtc.js uses timeout-guarded fetch for sign and pending control plane calls', () => {
  assert.match(RTC_SOURCE, /fetchJsonWithTimeout\(/);
  assert.doesNotMatch(RTC_SOURCE, /await fetch\(`\$\{ARGS\.signUrl\}/);
});

test('rtc.js registers dead intents into the skip list on definitive sign failure', () => {
  assert.match(RTC_SOURCE, /skippedIntents\.add\(/);
});
