// rtc-dnl4-balance.test.js —— 锁定「DNL4 账目必须按 reply 归零」这一行为契约。
//
// 为什么需要这个文件（2026-09-16）
// --------------------------------
// `rtc.js:47-51` 的注释自称已修掉一个错误口径：
//
//     「pacer.stats.sent/.dropped/.underruns 是**进程级累计、从不复位**，
//      而 arrived 在**每次 replyId 变化时归零**。不做同 reply 基准快照，
//      balance 就只在进程内第一个 reply 成立」
//
// 但这个"修好"本身**没有行为用例守着**。整条链是这样咬合的：
//
//   bridge.js:27-35 把 WS 消息的 reply_id 映射成 meta.replyId
//     → rtc.js:116 读 meta.replyId
//     → rtc.js:117 newReply = replyId !== null && replyId !== downProbe.replyId
//     → rtc.js:120-124 归零 arrived 并重快照 sentBase/droppedBase/underrunBase
//
// 任何一环坏掉（映射丢字段、判空条件被改、归零那几行被删），`newReply` 就恒为
// false：`downProbe.frames` 永不归零，DNL4 的 `arrived` 悄悄退回**进程级累计**口径，
// balance 只在第一个 reply 成立。此后「到底有没有丢帧」无法判断 —— 测量工具失真，
// 结论就无从谈起。而没有用例会变红。
//
// 本文件的做法
// ------------
// 抄 `barge-in-flush-exec.test.js` 已验证过的手法：用 `vm` 沙箱**按原字节**加载真实
// rtc.js，只把它 `require` 的**边界端口**换成替身：
//   · `./downlink_pacer` → 记录型 pacer（内部委托**真实** DownlinkPacer，只额外记录
//     构造/启动次数并暴露 tick 把手，供测试确定性推进节拍）
//   · `./bridge`         → 记录型 BridgeClient（把 onDownAudio / onCtrl 回调交出来供测试驱动）
//   · `./logger`         → 采集型 logger（把 DNL4 行抓下来解析）
//   · 其余（trtc sdk / config / security ...）→ 最小桩
// 然后走真实入口：rtc.js 末尾 `main()` → `startPollingRuntime({runSidecar})` →
// 真实的 `runSidecar()` 体，把 pacer 建出来、把下行回调注册进 BridgeClient。
// 测试再驱动那个注册进去的下行回调投帧，从 DNL4 日志行断言账目。
//
// 设计红线（不许退化）
// --------------------
//   · 不 patch 被测逻辑：rtc.js 源码**按原字节读入**，没有被裁剪或替换。
//   · 被替换的只是它 require 来的外部依赖，替换点全在被测对象的**边界**上。
//   · 断言的是 DNL4 行的**数值**（arrived / sent / pending / dropped / balance），
//     不是"某行代码存在"。把归零那几行删掉、或把 newReply 恒置 false，本文件必红。
//
// 运行：node --test test/rtc-dnl4-balance.test.js（在 sidecar/ 下）
// 硬约束：必须能在**没有三方原生依赖树**的 runner 上跑通（sidecar 门禁就是那种环境），
// 因此所有外部依赖一律走桩。
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SIDECAR_DIR = path.join(__dirname, '..');
const RTC_JS = path.join(SIDECAR_DIR, 'rtc.js');
const RTC_STARTUP_JS = path.join(SIDECAR_DIR, 'rtc-startup.js');

// 按**原字节**读入被测对象：不做任何文本替换，保证测的是真实源码。
const RTC_SOURCE = fs.readFileSync(RTC_JS, 'utf8');

const { DownlinkPacer: RealDownlinkPacer } = require(
  path.join(SIDECAR_DIR, 'downlink_pacer.js'),
);

const TEST_BRIDGE_URL = 'ws://127.0.0.1:1/bridge';
const TEST_CREDENTIAL = 'test-credential';

// pacer 的真实队列上限（downlink_pacer.js DEFAULTS.maxFrames = 50）。
// 一次投帧超过它就会触发"丢最旧"，那会污染 balance 断言 —— 测试必须在该预算内投帧。
const PACER_MAX_FRAMES = 50;

/**
 * 记录型 pacer 替身：**委托真实 DownlinkPacer**，只额外记录构造/启动次数，
 * 并暴露 `tick()` 把手。
 *
 * 为什么不是"纯计数桩"：纯桩的 push/tick 可以任意实现，于是「真实队列到底积了几帧」
 * 在断言上不可区分。委托真实实现后，`pending`/`stats.sent` 反映真实队列与计数，
 * balance 断言才有内容。
 *
 * 定时器注入为"永不定时"：本测试要确定性，不依赖真实时钟，也不让进程被定时器吊住。
 */
function makeRecordingPacer() {
  const rec = { instances: [], startCalls: 0, clearCalls: 0 };

  class RecordingPacer {
    constructor(opts) {
      assert.ok(opts && typeof opts.send === 'function',
        'rtc.js 必须以 send 回调构造 pacer（构造签名变了请同步本测试）');
      this._real = new RealDownlinkPacer({
        send: opts.send,
        onDrop: opts.onDrop,
        setIntervalImpl: () => 'FAKE_TIMER_HANDLE',
        clearIntervalImpl: () => {},
      });
      rec.instances.push(this);
    }

    push(buf) { this._real.push(buf); }

    start() { rec.startCalls += 1; this._real.start(); }

    stop() { this._real.stop(); }

    clear() {
      rec.clearCalls += 1;
      return this._real.clear();
    }

    /** 测试专用把手：按真实语义送一帧（空队列则记一次欠载）。 */
    tick() { return this._real.tick(); }

    get pending() { return this._real.pending; }

    get stats() { return this._real.stats; }
  }

  return { rec, RecordingPacer };
}

/** 在 vm 沙箱里加载**真实 rtc.js**，把它的边界端口换成替身，返回测试用的把手。 */
function loadRealRtcJs() {
  const { rec: pacerRec, RecordingPacer } = makeRecordingPacer();
  const captured = { ctrl: null, downAudio: null, onDisconnect: null, bridgeUrl: null };
  const logs = [];

  class RecordingBridgeClient {
    constructor(url, onDownAudio, onCtrl, onDisconnect) {
      captured.bridgeUrl = url;
      captured.downAudio = onDownAudio;
      captured.ctrl = onCtrl;
      captured.onDisconnect = onDisconnect;
    }
    startSession() {}
    refreshSession() {}
    clearSession() {}
    close() {}
    sendUpAudio() {}
    sendPeerState() {}
    noteTermination() { return false; }
    get connected() { return false; }
  }

  const cloud = {
    getTRTCShareInstance: () => cloud,
    enterRoom() {},
    exitRoom() {},
    stopLocalAudio() {},
    enableCustomAudioCapture() {},
    sendCustomAudioData() {},
    setAudioFrameCallback() {},
    enableAudioVolumeEvaluation() {},
    on() {},
    getSDKVersion: () => 'test-sdk-version',
  };

  const stubs = {
    './config': {
      ARGS: { role: 'sidecar', bridgeUrl: TEST_BRIDGE_URL, signUrl: 'http://127.0.0.1:1' },
      sidecarCredential: TEST_CREDENTIAL,
      SIDECAR_USER_ID: 'jax-pc-sidecar',
    },
    // 采集型 logger：DNL4 行是 DNL4 账目唯一的对外可观察出口，必须抓下来。
    './logger': () => (scope, msg) => { logs.push({ scope, msg: String(msg) }); },
    './downlink_pacer': { DownlinkPacer: RecordingPacer, DEFAULTS: { frameMs: 20, maxFrames: PACER_MAX_FRAMES } },
    './bridge': { BridgeClient: RecordingBridgeClient, sessionHello: (s) => s },
    './security': { controlPlaneHeaders: () => ({}) },
    './exit-protocol': { requestRendererExit: () => {} },
    // 真实 rtc-startup：它会在 try 内**同步调用 runSidecar()**，这正是我们要走的路。
    './rtc-startup': require(RTC_STARTUP_JS),
    './intent-selection': { selectPendingIntent: () => null },
    './intent-recovery': {
      createIntentSkipList: () => ({ add() {}, has: () => false }),
      createRecoveryState: () => ({
        consecutiveFailures: 0, recordFailure: () => true, reset() {},
      }),
      fetchJsonWithTimeout: async () => ({ code: 0, data: {} }),
    },
    './rtc-termination': { CMD_ID_TERMINATE: 1, makeTerminationCmdHandler: () => () => {} },
    './audio': { frameToS16Mono16k: () => null, makeAudioFrame16k: () => ({}) },
    './rtc-test-audio': { injectTestAudio: () => {} },
    // 本端设备表观测（2026-09-16 容器事故沉淀）：只出日志（scope=ADEV），
    // 不参与下行账目。本文件断言的是 DNL4 的账目自洽，与它无关。
    './adev': {
      PLAYER_DEVICE_EMPTY_CODE: 1202,
      inventory: () => ({ speakers: [], mics: [], speakersError: '', micsError: '' }),
      formatInventory: () => '[ADEV] stub',
      onWarningLine: () => ({ audioDevice: false, text: '[ADEV] stub' }),
    },
  };

  const fakeRequire = (id) => {
    if (id === 'trtc-electron-sdk') {
      // 真机 SDK：构造期就会去加载平台原生二进制，runner 上不存在。
      // 这里只补 rtc.js 真正用到的三个符号。
      return {
        default: cloud,
        TRTCParams: function TRTCParams() {},
        TRTCAppScene: { TRTCAppSceneAudioCall: 1 },
      };
    }
    if (Object.prototype.hasOwnProperty.call(stubs, id)) return stubs[id];
    throw new Error(
      `rtc.js 新引入了未被本测试打桩的依赖：${id} —— 请补桩，不要删除本测试`,
    );
  };

  const sandbox = {
    require: fakeRequire,
    module: { exports: {} },
    exports: {},
    console: { log() {}, error() {}, warn() {} },
    Buffer,
    process: {
      env: {},
      hrtime: Object.assign(
        (t) => process.hrtime(t),
        { bigint: () => process.hrtime.bigint() },
      ),
    },
    // 定时器全部空转：本测试是确定性的，不依赖真实时钟，也不让进程被定时器吊住。
    setInterval: () => 'FAKE_INTERVAL',
    clearInterval: () => {},
    setTimeout: () => 'FAKE_TIMEOUT',
    clearTimeout: () => {},
    Date, JSON, Math, Number, String, Boolean, Array, Object, Error, TypeError,
    RangeError, Promise, Symbol, Map, Set, WeakMap, Reflect, RegExp, Function,
    parseInt, parseFloat, isNaN, isFinite, Infinity, NaN, BigInt,
  };

  vm.createContext(sandbox);
  vm.runInContext(RTC_SOURCE, sandbox, { filename: RTC_JS });

  // 真实入口链：rtc.js 末尾 `main()` → rtc-startup `runSidecar()`。
  return { captured, pacerRec, logs };
}

/** 造一帧与链路一致的 640B 下行帧（20ms @16k s16 单声道）。 */
function frame(i) {
  return Buffer.alloc(640, i & 0xff);
}

/**
 * 解析一条 DNL4 日志行。
 *
 * 行格式（rtc.js:143-146；scope 为 DNL4，msg 本身以 reply= 开头）：
 *   reply=<r> seq=<n> src=<n> q=-1 arrived=<N> sent=<N> dropped=<N>
 *        pending=<N> underruns=<N> balance=<±N> L4=...
 */
function parseDnl4(msg) {
  const m = /^reply=(\S+) seq=(\S+) src=(\S+) q=(\S+) arrived=(\d+) sent=(-?\d+) dropped=(-?\d+) pending=(\d+) underruns=(\d+) balance=(-?\d+)/
    .exec(msg);
  if (!m) return null;
  return {
    reply: m[1],
    seq: m[2],
    arrived: Number(m[5]),
    sent: Number(m[6]),
    dropped: Number(m[7]),
    pending: Number(m[8]),
    underruns: Number(m[9]),
    balance: Number(m[10]),
  };
}

/** 取出本上下文里所有 DNL4 行（已解析）。解析不出即断言失败，避免 null 悄悄穿透成空转绿。 */
function dnl4Rows(ctx) {
  const lines = ctx.logs.filter((l) => l.scope === 'DNL4').map((l) => l.msg);
  const parsed = lines.map(parseDnl4);
  assert.ok(lines.length > 0, '一条 DNL4 都没有 —— DNL4 埋点被删了，账目已不可观测');
  assert.ok(parsed.every((r) => r !== null),
    `DNL4 行格式变了，解析失败：${lines.filter((_, i) => parsed[i] === null)[0]}`);
  return parsed;
}

/** 前置校验：确认测试确实挂上了真实 runSidecar 的端口（否则后面断言全是空转）。 */
function loadAndAssertWired() {
  const ctx = loadRealRtcJs();
  assert.strictEqual(ctx.pacerRec.instances.length, 1,
    'runSidecar() 必须恰好构造一个 pacer（0 个说明真实入口链没跑起来，测试会空转）');
  assert.strictEqual(ctx.pacerRec.startCalls, 1, 'runSidecar() 必须启动 pacer');
  assert.strictEqual(typeof ctx.captured.downAudio, 'function',
    'BridgeClient 必须收到下行音频回调（真实 rtc.js 的注册点被改过？）');
  assert.strictEqual(ctx.captured.bridgeUrl, TEST_BRIDGE_URL);
  return ctx;
}

/** 从 ctx 里取唯一的 pacer 实例把手。 */
function pacerOf(ctx) {
  assert.strictEqual(ctx.pacerRec.instances.length, 1);
  return ctx.pacerRec.instances[0];
}

// ── 核心行为断言：每个 reply 的账目都必须自洽 ─────────────────────────────────

test('两个不同 replyId 的账目都必须自洽：arrived 按 reply 归零、balance 恒为 0', () => {
  const ctx = loadAndAssertWired();
  const pacer = pacerOf(ctx);

  // ── reply r1 第一批：30 帧，不做 tick（模拟"rtc_bridge 突发下发"）──
  for (let i = 0; i < 30; i += 1) ctx.captured.downAudio(frame(i), { replyId: 'r1' });
  assert.strictEqual(pacer.pending, 30, '30 帧必须真的进入节拍器队列（前提校验）');

  let rows = dnl4Rows(ctx);
  assert.ok(rows.length >= 1, '首帧必须打 DNL4（首帧必打的约定被删了？）');
  assert.deepStrictEqual(
    { reply: rows[0].reply, arrived: rows[0].arrived, sent: rows[0].sent,
      pending: rows[0].pending, dropped: rows[0].dropped, balance: rows[0].balance },
    { reply: 'r1', arrived: 1, sent: 0, pending: 1, dropped: 0, balance: 0 },
    'r1 首帧：arrived=1 / sent=0 / pending=1 / dropped=0 / balance=0',
  );

  // ── 让节拍器把 r1 第一批全部播出去（真实 50 帧/s 的等价推进）──
  for (let i = 0; i < 30; i += 1) assert.strictEqual(pacer.tick(), true);
  assert.strictEqual(pacer.pending, 0, 'tick 30 次后队列必须为空');

  // ── reply r1 第二批：20 帧，累计 arrived 到 50（同 reply 不得归零）──
  for (let i = 0; i < 20; i += 1) ctx.captured.downAudio(frame(i), { replyId: 'r1' });
  rows = dnl4Rows(ctx);
  const r1At50 = rows.filter((r) => r.reply === 'r1' && r.arrived === 50).pop();
  assert.ok(r1At50, 'r1 必须在 arrived=50 处打出一条 DNL4（同 reply 内 arrived 必须持续累加）');
  assert.deepStrictEqual(
    { sent: r1At50.sent, pending: r1At50.pending, dropped: r1At50.dropped, balance: r1At50.balance },
    { sent: 30, pending: 20, dropped: 0, balance: 0 },
    'r1 第 50 帧：sent 必须是**本 reply 增量** 30（不是进程级累计）、pending=20、balance=0',
  );

  // ── 播完 r1，让队列归零（真实场景里新回复总在旧回复播完后到）──
  for (let i = 0; i < 20; i += 1) assert.strictEqual(pacer.tick(), true);
  assert.strictEqual(pacer.pending, 0);

  // ── reply r2：50 帧。这是本用例的**核心**：新 reply 必须归零 ──
  for (let i = 0; i < 50; i += 1) ctx.captured.downAudio(frame(i), { replyId: 'r2' });

  rows = dnl4Rows(ctx);
  const r2Rows = rows.filter((r) => r.reply === 'r2');
  const r2First = r2Rows.find((r) => r.arrived === 1);
  const r2At50 = r2Rows.filter((r) => r.arrived === 50).pop();

  assert.ok(r2First, 'r2 首帧必须打在 arrived=1 上 —— 若 arrived 是 51（=r1 的 50 + 1），'
    + '说明 downProbe.frames 没有按 reply 归零，DNL4 已退回进程级累计口径');
  assert.deepStrictEqual(
    { arrived: r2First.arrived, sent: r2First.sent, pending: r2First.pending,
      dropped: r2First.dropped, balance: r2First.balance },
    { arrived: 1, sent: 0, pending: 1, dropped: 0, balance: 0 },
    'r2 首帧：arrived 归零为 1、sent 为**本 reply 增量** 0（不是进程级累计的 50）、balance=0',
  );

  assert.ok(r2At50, 'r2 必须在 arrived=50 处再打一条 DNL4');
  assert.deepStrictEqual(
    { arrived: r2At50.arrived, sent: r2At50.sent, pending: r2At50.pending,
      dropped: r2At50.dropped, balance: r2At50.balance },
    { arrived: 50, sent: 0, pending: 50, dropped: 0, balance: 0 },
    'r2 第 50 帧：arrived=50、sent=0（增量口径）、pending=50、balance=0',
  );

  // ── 全局不变量：**每一条** DNL4 行的 balance 都必须为 0 ──
  for (const r of rows) {
    assert.strictEqual(r.balance, r.arrived - r.sent - r.pending - r.dropped,
      `DNL4 行内部算术必须自洽：${JSON.stringify(r)}`);
    assert.strictEqual(r.balance, 0,
      `任何一条 DNL4 行的 balance 都必须为 0（arrived − sent − pending − dropped === 0）：${JSON.stringify(r)}`);
  }
});

test('同一 replyId 连续投帧不得归零（防"无条件归零"这一种反向假绿）', () => {
  // 与上一条互为反向：上一条防"该归零不归零"，这条防"不该归零却归零"。
  // 若有人把 `newReply` 写成恒 true（例如删掉 replyId !== downProbe.replyId 的判等），
  // 上一条的 arrived=1 仍绿，但 arrived 永远停在 1、再也不会打到 50 —— 只有本用例会红。
  const ctx = loadAndAssertWired();
  const pacer = pacerOf(ctx);

  // 分批投 50 帧，全部同一个 replyId。
  for (let i = 0; i < 10; i += 1) ctx.captured.downAudio(frame(i), { replyId: 'same' });
  assert.strictEqual(pacer.tick(), true);
  for (let i = 0; i < 40; i += 1) ctx.captured.downAudio(frame(i), { replyId: 'same' });

  const rows = dnl4Rows(ctx).filter((r) => r.reply === 'same');
  const at50 = rows.filter((r) => r.arrived === 50).pop();
  assert.ok(at50,
    '同一 replyId 内的 arrived 必须持续累加到 50；到达不了 50 说明每帧都在归零（归零条件写错了）');
  assert.strictEqual(at50.pending, 49, 'sent 1 帧后队列应剩 49 帧');
  assert.strictEqual(at50.sent, 1, 'pending 必须来自本 reply 增量 sent=1');
  assert.strictEqual(at50.balance, 0, 'balance 必须为 0');
});

test('缺 replyId 时不得归零、不得抛错（旧版 rtc_bridge 兼容）', () => {
  // meta 缺失是**允许且已声明**的降级路径（bridge.js:23-26）。此时 replyId 为 null，
  // rtc.js:117 的 newReply 恒 false ⇒ 这些帧并入"上一个 reply 的累计"，绝不归零 ——
  // 否则每一帧都归零，账目会彻底失去意义。同时不得抛错（抛错 = 下行整体中断）。
  const ctx = loadAndAssertWired();
  const pacer = pacerOf(ctx);

  assert.doesNotThrow(() => {
    for (let i = 0; i < 50; i += 1) ctx.captured.downAudio(frame(i), undefined);
  }, 'meta 缺失时下行回调不得抛错');

  const rows = dnl4Rows(ctx).filter((r) => r.reply === '-');
  const at50 = rows.filter((r) => r.arrived === 50).pop();
  assert.ok(at50, '缺 replyId 时 arrived 必须持续累加（reply 显示为 `-`），不得每帧归零');
  assert.strictEqual(at50.balance, 0, 'balance 仍必须为 0');
  assert.strictEqual(pacer.stats.sent, 0, '本用例不推进节拍，不应有帧被送出');
});
