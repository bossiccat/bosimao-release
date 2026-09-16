// down-audio-meta.test.js —— 锁定 `bridge.js` 的 down_audio 元数据映射契约。
//
// 为什么需要这个文件
// ------------------
// `sidecar/bridge.js:27-35` 的 `downAudioMeta()` 把 rtc_bridge 经 localhost WS 推来的
// 蛇形字段（reply_id / frame_seq / src_seq / t_enq / t_send）映射为驼峰
// （replyId / frameSeq / srcSeq / tEnq / tSend），供 `sidecar/rtc.js:116-141` 的 DNL4
// 对账消费。这是**下行链路跨进程那一跳的消费者边界**，此前零测试覆盖：
//
//   · 若映射被写坏（例如把 `message.reply_id` 误改成 `message.replyId`），
//     rtc.js 侧拿到的 `meta.replyId` 恒为 `undefined` ⇒ `replyId` 恒为 `null`
//     ⇒ `newReply`（rtc.js:117）恒为 false ⇒ `downProbe.frames` 永不归零。
//   · 后果：DNL4 的 `arrived` 退回「进程级累计」口径 —— 正是 rtc.js:47-51 注释
//     自称已修掉的那个错误口径 —— 而**没有任何用例会变红**，测量结论失真却无人察觉。
//
// 做法
// ----
// 不改生产代码、也不为了测试把它导出：走**真实消息端口**。
// `BridgeClient._onMessage(event)`（bridge.js:113-123）是普通方法，对 down_audio 消息
// 会执行 `this.onDownAudio(Buffer.from(message.pcm_b64,'base64'), downAudioMeta(message))`。
// 测试注入 spy 作为 onDownAudio，直接调 `_onMessage` 驱动，断言**第二个实参恰为**
// 映射结果。这样连 `pcm_b64` 解码与 `type` 分派一起验，覆盖面大于单独测映射函数。
//
// 运行：node --test test/down-audio-meta.test.js（在 sidecar/ 下）
// 硬约束：本文件**零外部依赖**——必须在没有三方原生依赖树的环境里也能跑通
// （sidecar 门禁就是那种环境），因此只 require 本仓 sidecar/ 下的纯 JS。
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const { BridgeClient } = require(path.join(__dirname, '..', 'bridge.js'));

const PCM_640 = Buffer.alloc(640, 7); // 20ms @16k mono s16
const PCM_B64 = PCM_640.toString('base64');
const BRIDGE_URL = 'ws://127.0.0.1:19092/bridge';

/** 记录型 onDownAudio：把每次调用的两个实参原样留下供断言。 */
function makeClient() {
  const downCalls = [];
  const ctrlCalls = [];
  const bc = new BridgeClient(
    BRIDGE_URL,
    (buf, meta) => { downCalls.push({ buf, meta }); },
    (action, reason) => { ctrlCalls.push({ action, reason }); },
  );
  return { bc, downCalls, ctrlCalls };
}

/** 造一条真实形状的 down_audio 消息（rtc_bridge 侧字段全带）。 */
function downAudioMessage(overrides) {
  return JSON.stringify({
    type: 'down_audio',
    pcm_b64: PCM_B64,
    reply_id: 'r1',
    frame_seq: 3,
    src_seq: 1,
    t_enq: 1.5,
    t_send: 1.6,
    ...overrides,
  });
}

// ── 核心行为断言：蛇形 → 驼峰的映射必须原样发生 ────────────────────────────────

test('down_audio 必须映射为 camelCase 元数据并解码 pcm_b64（行为断言）', () => {
  const { bc, downCalls } = makeClient();

  bc._onMessage({ data: downAudioMessage() });

  assert.strictEqual(downCalls.length, 1, 'down_audio 必须恰好回调 onDownAudio 一次');

  const { buf, meta } = downCalls[0];
  assert.ok(Buffer.isBuffer(buf), '第一个实参必须是 Buffer（不是 base64 字符串）');
  assert.strictEqual(buf.length, 640, '必须按 base64 解出原始 640 字节');
  assert.ok(buf.equals(PCM_640), '解码后的字节必须与发送端逐字节一致');

  // 用 deepStrictEqual 而非逐键断言：键集合也必须**恰好**是这 5 个。
  // 若映射函数悄悄加/漏一个键，或写成嵌套结构，这里都会红。
  assert.deepStrictEqual(meta, {
    replyId: 'r1',
    frameSeq: 3,
    srcSeq: 1,
    tEnq: 1.5,
    tSend: 1.6,
  }, '第二个实参必须恰为 snake→camel 映射结果');
});

test('映射必须逐字段独立：只带部分字段时其余键为 undefined（不得串位）', () => {
  const { bc, downCalls } = makeClient();

  // 只带 2 个字段（其余**不出现**，模拟旧版 rtc_bridge 的部分升级）。
  bc._onMessage({ data: JSON.stringify({
    type: 'down_audio', pcm_b64: PCM_B64, reply_id: 'r-partial', frame_seq: 9,
  }) });

  assert.deepStrictEqual(downCalls[0].meta, {
    replyId: 'r-partial',
    frameSeq: 9,
    srcSeq: undefined,
    tEnq: undefined,
    tSend: undefined,
  }, '未提供的字段必须降级为 undefined，且不得被其它字段顶替（串位即静默错账）');
});

// ── 边界：旧版 rtc_bridge 不带这些字段 → 降级，不抛错 ─────────────────────────

test('字段全部缺失时降级为 undefined，绝不抛错（旧版 rtc_bridge 兼容）', () => {
  const { bc, downCalls } = makeClient();

  assert.doesNotThrow(() => bc._onMessage({
    data: JSON.stringify({ type: 'down_audio', pcm_b64: PCM_B64 }),
  }), '旧版 rtc_bridge 不带元数据字段，映射不得抛错（否则下行整体中断）');

  assert.strictEqual(downCalls.length, 1, '元数据缺失不得导致丢帧：仍须回调一次');
  assert.deepStrictEqual(downCalls[0].meta, {
    replyId: undefined,
    frameSeq: undefined,
    srcSeq: undefined,
    tEnq: undefined,
    tSend: undefined,
  }, '缺失字段必须降级为 undefined');
});

test('字段类型不符时降级为 undefined，绝不抛错（畸形输入不得污染映射）', () => {
  const { bc, downCalls } = makeClient();

  bc._onMessage({ data: downAudioMessage({
    reply_id: 7,        // 非 string
    frame_seq: 3.5,     // 非 integer
    src_seq: null,      // 非 integer
    t_enq: '1.5',       // 非 number
    t_send: NaN,        // 非 finite（JSON 里是 null，这里直接走对象路径）
  }) });

  assert.deepStrictEqual(downCalls[0].meta, {
    replyId: undefined,
    frameSeq: undefined,
    srcSeq: undefined,
    tEnq: undefined,
    tSend: undefined,
  }, '类型不符的字段必须降级为 undefined —— 带毒的值进入 DNL4 会让账目静默错乱');
});

test('reply_id 为空字符串时按"有值"透传（长度为 0 的字符串仍是合法 reply 键）', () => {
  // 说明：映射只判 `typeof === 'string'`，不判非空。这不是缺陷——空串 replyId 会在
  // rtc.js 侧被当成一个合法的 reply 标识（`replyId !== null`），从而触发归零。
  // 本用例把这一既有语义钉住，防止有人"顺手"改成非空判断而悄悄改变归零时机。
  const { bc, downCalls } = makeClient();
  bc._onMessage({ data: downAudioMessage({ reply_id: '' }) });
  assert.strictEqual(downCalls[0].meta.replyId, '', '空串 reply_id 必须原样透传');
});

// ── 反向：非 down_audio 消息不得触发下行回调 ─────────────────────────────────

test('非 down_audio 消息不得触发 onDownAudio（分派必须按 type 收口）', () => {
  const { bc, downCalls, ctrlCalls } = makeClient();

  bc._onMessage({ data: JSON.stringify({ type: 'up_audio', pcm_b64: PCM_B64 }) });
  bc._onMessage({ data: JSON.stringify({ type: 'down_audio' }) }); // 无 pcm_b64
  bc._onMessage({ data: JSON.stringify({ type: 'ctrl', action: 'flush_downlink' }) });
  bc._onMessage({ data: 'not json at all' });
  bc._onMessage({ data: Buffer.from('binary-frame') }); // 非 string data

  assert.strictEqual(downCalls.length, 0,
    '只有带 pcm_b64 的 down_audio 才允许触发下行注入（否则会出现空帧注入）');
  assert.deepStrictEqual(ctrlCalls, [{ action: 'flush_downlink', reason: '' }],
    'ctrl 消息必须仍走 onCtrl 分派');
});
