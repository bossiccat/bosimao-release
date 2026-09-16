// barge-in-flush-exec.test.js —— 锁定「打断冲刷必须真的落到节拍器队列」这一行为契约。
//
// 为什么需要这个文件（2026-09-16）
// --------------------------------
// 原契约 `backend/tests/contract/test_barge_in_flush_contract.py::
// test_sidecar_executes_flush_on_pacer` 是**源码字符串扫描**：
//
//     assert "flush_downlink" in src
//     assert "pacer.clear()" in src
//
// 这种断言**对行为的改变完全无感**：把 rtc.js 里的
//     if (action === 'flush_downlink') { ... pacer.clear() ... }
// 改成
//     if (action === 'flush_downlink' && false) { ... pacer.clear() ... }
// 字符串依旧全在 ⇒ 测试照样绿，而线上打断冲刷已经彻底失效（旧回复会把节拍器里
// 最多 1 秒的积压播完，正是实测打断延迟 1.75s 的主要来源）。字符串扫描看见的是
// 「代码里存在这句话」，我们要守的是「这句话在收到 ctrl 时真的执行了」。
//
// 本文件的做法
// ------------
// 用 `vm` 沙箱**加载真实的 rtc.js**（不复制逻辑、不 if 白名单），只把它的**边界端口**
// 换成记录型替身：
//   · `./downlink_pacer` → 记录型 pacer（内部委托**真实** DownlinkPacer，记录 clear() 调用）
//   · `./bridge`         → 记录型 BridgeClient（把 ctrl 回调原样交出来供测试驱动）
//   · `trtc-electron-sdk`/`./config`/`./logger` 等 → 最小桩（真实 rtc.js 在无 Electron、
//     无三方原生依赖的环境下无法直接 require）
// 然后走真实入口：rtc.js 末尾的 `main()` → `startPollingRuntime({runSidecar})` → 真实的
// `runSidecar()` 体，把 pacer 建出来、把 ctrl 回调注册进 BridgeClient。测试再驱动那个
// 注册进去的回调，断言**队列真的从 30 帧变成 0 帧**。
//
// 设计红线（不许退化）
// --------------------
//   · 不 patch 被测逻辑：rtc.js 源码**按原字节读入**、没有被裁剪或替换。
//   · 被替换的只是它 require 来的外部依赖，且替换点都在被测对象的**边界**上。
//   · 断言的是队列状态（`pacer.pending`）这一**可观察行为**，不是「clear 这个方法名
//     被调用过」——记录型替身内部委托真实 DownlinkPacer，所以「调用了 clear 但没清队列」
//     这类假实现同样会被抓红。
//
// 运行：node --test test/barge-in-flush-exec.test.js（在 sidecar/ 下）
// 注意：本文件必须能在**没有三方原生依赖树**的 runner 上跑通（CI 的 sidecar 门禁就是
// 这种环境），因此所有外部依赖一律走桩。
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

/**
 * 记录型 pacer 替身：**委托真实 DownlinkPacer**，只额外记录 clear() 被调用了几次。
 *
 * 为什么不是"纯计数桩"：纯桩的 clear() 可以返回任意值，于是「rtc.js 调了 clear 但
 * 真实实现没清队列」与「真的清了」在断言上无法区分。委托真实实现后，`pending`
 * 反映的是真实队列长度，断言才有内容。
 */
function makeRecordingPacer() {
  const rec = { instances: [], startCalls: 0, clearCalls: 0, lastClearReturn: undefined };

  class RecordingPacer {
    constructor(opts) {
      assert.ok(opts && typeof opts.send === 'function',
        'rtc.js 必须以 send 回调构造 pacer（构造签名变了请同步本测试）');
      // 真实实现，但注入"永不定时"的定时器：本测试要确定性，不依赖真实时钟。
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
      const n = this._real.clear();
      rec.lastClearReturn = n;
      return n;
    }

    get pending() { return this._real.pending; }

    get stats() { return this._real.stats; }
  }

  return { rec, RecordingPacer };
}

/**
 * 在 vm 沙箱里加载**真实 rtc.js**，把它的边界端口换成替身，返回测试用的把手。
 */
function loadRealRtcJs() {
  const { rec: pacerRec, RecordingPacer } = makeRecordingPacer();
  const captured = { ctrl: null, downAudio: null, onDisconnect: null, bridgeUrl: null };

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
    './logger': () => () => {},
    './downlink_pacer': { DownlinkPacer: RecordingPacer, DEFAULTS: { frameMs: 20, maxFrames: 50 } },
    './bridge': { BridgeClient: RecordingBridgeClient, sessionHello: (s) => s },
    './security': { controlPlaneHeaders: () => ({}) },
    './exit-protocol': { requestRendererExit: () => {} },
    // 真实 rtc-startup：它会在 try 内**同步调用 runSidecar()**，这正是我们要走的路。
    // 只把它传入的 scheduleInterval/scheduleTimeout 换成沙箱里的空实现（见下）。
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
    // 本端设备表观测（2026-09-16 容器事故沉淀）：纯函数模块，这里只需形状正确。
    // 它只影响日志，不得影响打断冲刷行为 —— 本文件断言的正是后者。
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

  // 真实入口链：rtc.js:448 `main()` → rtc-startup:35 `runSidecar()`。
  return { captured, pacerRec };
}

/** 造一帧与链路一致的 640B 下行帧（20ms @16k s16 单声道）。 */
function frame(i) {
  return Buffer.alloc(640, i & 0xff);
}

/** 前置校验：确认测试确实挂上了真实 runSidecar 的端口（否则后面的断言是空转的）。 */
function loadAndAssertWired() {
  const ctx = loadRealRtcJs();
  assert.strictEqual(ctx.pacerRec.instances.length, 1,
    'runSidecar() 必须恰好构造一个 pacer（0 个说明真实入口链没跑起来，测试会空转）');
  assert.strictEqual(ctx.pacerRec.startCalls, 1, 'runSidecar() 必须启动 pacer');
  assert.strictEqual(typeof ctx.captured.ctrl, 'function',
    'BridgeClient 必须收到 ctrl 回调（真实 rtc.js 的注册点被改过？）');
  assert.strictEqual(typeof ctx.captured.downAudio, 'function',
    'BridgeClient 必须收到下行音频回调');
  assert.strictEqual(ctx.captured.bridgeUrl, TEST_BRIDGE_URL);
  return ctx;
}

// ── 核心行为断言：打断冲刷必须真的清空节拍器队列 ──────────────────────────────

test('收到 flush_downlink ctrl 时，节拍器队列必须真的被清空（行为断言）', () => {
  const { captured, pacerRec } = loadAndAssertWired();

  // 1) 先制造"被打断时真实存在"的积压：模拟 rtc_bridge 突发下发 30 帧。
  for (let i = 0; i < 30; i += 1) captured.downAudio(frame(i), { replyId: 'r1' });
  assert.strictEqual(pacerRec.instances[0].pending, 30,
    '下行帧必须真的进入节拍器队列（否则本用例的前提不成立）');

  // 2) 驱动真实 ctrl 回调（rtc.js 注册给 BridgeClient 的那个）。
  captured.ctrl('flush_downlink', '');

  // 3) 断言可观察行为：队列真的空了，且 clear() 真的被调用了一次。
  assert.strictEqual(pacerRec.instances[0].pending, 0,
    '收到 flush_downlink 后节拍器队列必须为空——否则被打断的旧回复会把最多 1 秒积压播完');
  assert.strictEqual(pacerRec.clearCalls, 1,
    'flush_downlink 必须真的调到 pacer.clear()（一次）');
  assert.strictEqual(pacerRec.lastClearReturn, 30,
    'clear() 的返回值必须是被丢弃的真实帧数（替身不得伪造这个数）');
});

test('非 flush_downlink 的 ctrl 不得清空队列（无条件的 clear 是另一种假绿）', () => {
  // 与上一条互为反向：上一条防"该清不清"，这条防"不该清却清"。
  // 若有人把 `if (action === 'flush_downlink')` 的守卫整体去掉、改成无条件
  // `pacer.clear()`，上一条仍绿，只有本条会红。
  const { captured, pacerRec } = loadAndAssertWired();

  for (let i = 0; i < 30; i += 1) captured.downAudio(frame(i), { replyId: 'r1' });
  assert.strictEqual(pacerRec.instances[0].pending, 30);

  // 发一个**同样流经该 ctrl 回调**但语义不同的 action。
  captured.ctrl('test_audio', '');

  assert.strictEqual(pacerRec.instances[0].pending, 30,
    '只有 flush_downlink 才允许清队列；其它 action 触发清空 = 无条件打断（另一种假绿）');
  assert.strictEqual(pacerRec.clearCalls, 0,
    '非 flush_downlink 的 ctrl 不得触碰 pacer.clear()');
});

// ── 边界：重复冲刷 / 空队列冲刷 ────────────────────────────────────────────────

test('重复 flush_downlink 各自都要执行一次（不得只清第一次）', () => {
  const { captured, pacerRec } = loadAndAssertWired();

  for (let i = 0; i < 30; i += 1) captured.downAudio(frame(i), { replyId: 'r1' });
  captured.ctrl('flush_downlink', '');
  assert.strictEqual(pacerRec.instances[0].pending, 0);

  // 第二次打断前又有新的积压（下一个 reply）。
  for (let i = 0; i < 12; i += 1) captured.downAudio(frame(i), { replyId: 'r2' });
  assert.strictEqual(pacerRec.instances[0].pending, 12);
  captured.ctrl('flush_downlink', '');

  assert.strictEqual(pacerRec.instances[0].pending, 0, '第二次打断同样必须清空队列');
  assert.strictEqual(pacerRec.clearCalls, 2, '每次 flush_downlink 都必须执行一次 clear()');
  assert.strictEqual(pacerRec.lastClearReturn, 12, '第二次丢弃数应为真实的 12 帧');
});

test('空队列上收到 flush_downlink 必须安全（不抛、不虚增）', () => {
  const { captured, pacerRec } = loadAndAssertWired();

  assert.doesNotThrow(() => captured.ctrl('flush_downlink', ''),
    '空队列中断打不得抛错（打断可能在任何时刻到达）');
  assert.strictEqual(pacerRec.clearCalls, 1);
  assert.strictEqual(pacerRec.lastClearReturn, 0, '空队列丢弃数必须为 0');
  assert.strictEqual(pacerRec.instances[0].stats.dropped, 0, '空队列冲刷不得虚增 dropped');
});
