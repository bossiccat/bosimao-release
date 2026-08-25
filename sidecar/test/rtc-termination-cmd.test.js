'use strict';
// 手机端 → sidecar 的 onRecvCustomCmdMsg 终止通知处理测试（Task #23 上游接线）。
//
// 链路：RtcClient.sendTerminationNotice(tid)（cmdId=1, reliable, ordered）
//       → makeTerminationCmdHandler → bridge.noteTermination(currentSessionId, tid)
//       → 既有 bridge WS ctrl 上行 → rtc_bridge 注入注册表。
// fail-safe：非 cmdId=1 / 畸形 JSON / 非 note_termination / 非法 tid / 对端离开后
//            currentSessionId 已清空 → 不发送、不崩、不影响通话主链路。

const test = require('node:test');
const assert = require('node:assert/strict');
const { CMD_ID_TERMINATE, makeTerminationCmdHandler } = require('../rtc-termination');

const HELLO = {
  type: 'hello', proof: 'p', nonce: 'nonce-0123456789abcdef', jti: 'j',
  session_id: 's1', device_id: 'd1', room_id: 'r1',
  sidecar_user_id: 'jax-pc-sidecar', generation: 0, protocol_version: '1.0',
};

function fakeBridge() {
  return {
    noteTerminationCalls: [],
    ws: { sent: [] },
    noteTermination(sessionId, terminationId) {
      this.noteTerminationCalls.push([sessionId, terminationId]);
      // 模拟 BridgeClient 真实校验：非法参数返回 false 且不发送
      if (typeof sessionId !== 'string' || !sessionId) return false;
      if (typeof terminationId !== 'string' || !terminationId) return false;
      this.ws.sent.push(JSON.stringify({ sessionId, terminationId }));
      return true;
    },
  };
}

function harness({ bridge = fakeBridge(), sessionId = 's1' } = {}) {
  const logs = [];
  const handler = makeTerminationCmdHandler(
    () => bridge,
    () => sessionId,
    (tag, msg) => logs.push(`${tag}: ${msg}`)
  );
  return { handler, bridge, logs };
}

function utf8Payload(obj) {
  return Buffer.from(JSON.stringify(obj), 'utf8');
}

test('CMD_ID_TERMINATE is aligned with Android RtcClient.CMD_ID_TERMINATE', () => {
  assert.equal(CMD_ID_TERMINATE, 1);
});

test('valid cmdId=1 note_termination relays to bridge with current session id', () => {
  const { handler, bridge } = harness();
  handler('jax-device-1', CMD_ID_TERMINATE, 0,
    utf8Payload({ type: 'note_termination', termination_id: 'tid-9' }));
  assert.deepEqual(bridge.noteTerminationCalls, [['s1', 'tid-9']]);
  assert.equal(bridge.ws.sent.length, 1);
});

test('non-string binary payload (ArrayBuffer-like) is decoded and relayed', () => {
  const { handler, bridge } = harness();
  const view = new Uint8Array(utf8Payload({ type: 'note_termination', termination_id: 'tid-b' }));
  handler('u', CMD_ID_TERMINATE, 0, view);
  assert.deepEqual(bridge.noteTerminationCalls[0], ['s1', 'tid-b']);
});

test('other cmdIds are ignored without touching the bridge', () => {
  const { handler, bridge } = harness();
  handler('u', 2, 0, utf8Payload({ type: 'note_termination', termination_id: 'tid-x' }));
  assert.equal(bridge.noteTerminationCalls.length, 0);
  assert.equal(bridge.ws.sent.length, 0);
});

test('malformed payloads never crash the handler (fail-safe)', () => {
  const { handler, bridge } = harness();
  const malformed = [
    Buffer.from('not json {{{'),
    utf8Payload({ type: 'other_action' }),
    utf8Payload(null),
    utf8Payload('string body'),
    utf8Payload({ type: 'note_termination' }),                       // 缺 tid
    utf8Payload({ type: 'note_termination', termination_id: '' }),   // 空 tid
    utf8Payload({ type: 'note_termination', termination_id: 7 }),    // 非字符串
    utf8Payload({ type: 'note_termination', termination_id: 't'.repeat(129) }), // 超长
    new Uint8Array(0),                                               // 空字节
  ];
  for (const message of malformed) {
    assert.doesNotThrow(() => handler('u', CMD_ID_TERMINATE, 0, message));
  }
  assert.equal(bridge.noteTerminationCalls.length, 0);
  assert.equal(bridge.ws.sent.length, 0);
});

test('missing bridge is a silent no-op', () => {
  const { handler } = harness({ bridge: null });
  assert.doesNotThrow(() =>
    handler('u', CMD_ID_TERMINATE, 0,
      utf8Payload({ type: 'note_termination', termination_id: 'tid-9' })));
});

test('after peer leave clears session id, relay degrades to no-op (no send)', () => {
  // 模拟对端离开后 currentSessionId 已被 rtc.js 清空（null）
  const { handler, bridge } = harness({ sessionId: null });
  const sent = handler('u', CMD_ID_TERMINATE, 0,
    utf8Payload({ type: 'note_termination', termination_id: 'tid-late' }));
  assert.equal(sent, undefined); // 处理器无返回值；关键断言在下方
  assert.deepEqual(bridge.noteTerminationCalls.at(-1), [null, 'tid-late']);
  // 桥校验拦截非法 sessionId：不得有任何 WS 上行
  assert.equal(bridge.ws.sent.length, 0);
});
