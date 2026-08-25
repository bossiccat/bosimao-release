'use strict';
// sidecar → rtc_bridge WS ctrl「note_termination」中继行为测试。
//
// 链路：terminate 调用方获知 tid → （上游接入点调用 noteTermination）
//       → BridgeClient 经既有 bridge WS 上行 ctrl → rtc_bridge 注入注册表。
// fail-safe：socket 未开/未握手/参数非法 → 不发送、不抛错、不影响拆链主流程。

const test = require('node:test');
const assert = require('node:assert');
const { BridgeClient } = require('../bridge');

const HELLO = {
  type: 'hello', proof: 'p', nonce: 'nonce-0123456789abcdef', jti: 'j',
  session_id: 's1', device_id: 'd1', room_id: 'r1',
  sidecar_user_id: 'jax-pc-sidecar', generation: 0, protocol_version: '1.0',
};

function fakeSocket(readyState) {
  return { readyState, sent: [], send(x) { this.sent.push(x); } };
}

function connectedClient(readyState = WebSocket.OPEN) {
  const bc = new BridgeClient('ws://127.0.0.1:19092', () => {}, () => {});
  bc._activeHello = HELLO;
  bc.ws = fakeSocket(readyState);
  return bc;
}

test('noteTermination relays ctrl payload over open bridge socket', () => {
  const bc = connectedClient();
  assert.equal(bc.noteTermination('s1', 'tid-9'), true);
  assert.equal(bc.ws.sent.length, 1);
  assert.deepEqual(JSON.parse(bc.ws.sent[0]), {
    type: 'ctrl', action: 'note_termination',
    session_id: 's1', termination_id: 'tid-9',
  });
});

test('noteTermination is a no-op when socket is not open', () => {
  for (const readyState of [WebSocket.CLOSED, WebSocket.CONNECTING, WebSocket.CLOSING]) {
    const bc = connectedClient(readyState);
    assert.equal(bc.noteTermination('s1', 'tid-9'), false);
    assert.equal(bc.ws.sent.length, 0);
  }
});

test('noteTermination is a no-op without an active session hello', () => {
  const bc = new BridgeClient('ws://127.0.0.1:19092', () => {}, () => {});
  bc.ws = fakeSocket(WebSocket.OPEN);
  assert.equal(bc.noteTermination('s1', 'tid-9'), false);
  assert.equal(bc.ws.sent.length, 0);
});

test('noteTermination rejects malformed arguments without sending', () => {
  const bc = connectedClient();
  for (const [sid, tid] of [
    ['', 't'], ['s1', ''], ['s1', null], [null, 't'],
    [7, 't'], ['s1', 7], ['s1', { x: 1 }], ['s1', 't'.repeat(129)],
    [undefined, undefined],
  ]) {
    assert.equal(bc.noteTermination(sid, tid), false, `args=${sid},${tid}`);
  }
  assert.equal(bc.ws.sent.length, 0);
});

test('noteTermination survives socket send throwing (fail-safe)', () => {
  const bc = new BridgeClient('ws://127.0.0.1:19092', () => {}, () => {});
  bc._activeHello = HELLO;
  bc.ws = { readyState: WebSocket.OPEN, send() { throw new Error('socket gone'); } };
  assert.equal(bc.noteTermination('s1', 'tid-9'), false);
});

test('downlink ctrl routing regression: exit still reaches onCtrl', () => {
  let seen = null;
  const bc = new BridgeClient('ws://127.0.0.1:19092', () => {}, (action, reason) => {
    seen = { action, reason };
  });
  bc._onMessage({ data: JSON.stringify({ type: 'ctrl', action: 'exit', reason: 'device_revoked' }) });
  assert.deepEqual(seen, { action: 'exit', reason: 'device_revoked' });
});
