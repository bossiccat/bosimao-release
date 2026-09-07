'use strict';

const log = require('./logger');

function sessionHello(session) {
  const requiredStrings = [
    'proof', 'nonce', 'jti', 'session_id', 'device_id', 'room_id', 'sidecar_user_id',
  ];
  const validAudio = session && session.audio_format
    && session.audio_format.encoding === 'pcm_s16le'
    && session.audio_format.sample_rate_hz === 16000
    && session.audio_format.channels === 1
    && session.audio_format.frame_ms === 20
    && session.audio_format.frame_bytes === 640;
  if (!session || session.type !== 'hello' || session.protocol_version !== '1.0'
      || !Number.isInteger(session.generation) || session.generation < 0 || !validAudio
      || requiredStrings.some((key) => typeof session[key] !== 'string' || !session[key])) {
    throw new Error('SIDECAR_INVALID_SESSION_HELLO');
  }
  return { ...session };
}

// 下行帧元数据（rtc_bridge 侧 2026-09-07 新增的可选字段）。
// 旧版 rtc_bridge 不含这些字段 —— 一律降级为 undefined，绝不抛错、绝不丢帧。
// t_enq / t_send 是 rtc_bridge 进程的 monotonic 时钟，与 sidecar 时钟不同基准，
// 只能由 rtc_bridge 自己在其进程内做差，sidecar 侧仅原样透传。
function downAudioMeta(message) {
  return {
    replyId: typeof message.reply_id === 'string' ? message.reply_id : undefined,
    frameSeq: Number.isInteger(message.frame_seq) ? message.frame_seq : undefined,
    srcSeq: Number.isInteger(message.src_seq) ? message.src_seq : undefined,
    tEnq: Number.isFinite(message.t_enq) ? message.t_enq : undefined,
    tSend: Number.isFinite(message.t_send) ? message.t_send : undefined,
  };
}

class BridgeClient {
  constructor(url, onDownAudio, onCtrl, onDisconnect = () => {}) {
    this.url = url;
    this.onDownAudio = onDownAudio;
    this.onCtrl = onCtrl;
    this.onDisconnect = onDisconnect;
    this.ws = null;
    this.connected = false;
    this._stop = false;
    this._backoffIdx = 0;
    this._activeHello = null;
    this._sessionKey = null;
    this._generation = 0;
  }

  startSession(session) {
    const hello = sessionHello(session);
    const sessionKey = `${hello.session_id}\u0000${hello.device_id}\u0000${hello.room_id}`;
    this._stop = false;
    if (sessionKey === this._sessionKey) return;
    this._activeHello = hello;
    this._sessionKey = sessionKey;
    this._generation += 1;
    const staleSocket = this.ws;
    this.ws = null;
    this.connected = false;
    try { if (staleSocket) staleSocket.close(); } catch (_) { /* closed */ }
    this._connect(this._generation);
  }

  refreshSession(session) {
    this.startSession(session);
  }

  clearSession() {
    this._activeHello = null;
    this._sessionKey = null;
    this._generation += 1;
    this._backoffIdx = 0;
    const ws = this.ws;
    this.ws = null;
    this.connected = false;
    try { if (ws) ws.close(); } catch (_) { /* closed */ }
  }

  _connect(generation) {
    if (this._stop || !this._activeHello || this.ws || generation !== this._generation) return;
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      if (generation !== this._generation || this.ws !== ws || !this._activeHello) {
        try { ws.close(); } catch (_) { /* closed */ }
        return;
      }
      this.connected = true;
      this._backoffIdx = 0;
      ws.send(JSON.stringify(this._activeHello));
      log('WS', 'rtc_bridge connected');
    };
    ws.onmessage = (event) => {
      if (generation === this._generation && this.ws === ws) this._onMessage(event);
    };
    ws.onclose = () => {
      if (generation !== this._generation || this.ws !== ws) return;
      this.ws = null;
      this.connected = false;
      const hadSession = this._activeHello !== null;
      this._activeHello = null;
      this._sessionKey = null;
      this._generation += 1;
      this._backoffIdx = 0;
      if (hadSession && !this._stop) this.onDisconnect();
    };
    ws.onerror = () => { try { ws.close(); } catch (_) { /* closed */ } };
  }

  _onMessage(event) {
    if (typeof event.data !== 'string') return;
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === 'down_audio' && message.pcm_b64) {
      // meta 作为第二个参数透传（跨进程关联 ID：reply_id / frame_seq / src_seq）。
      this.onDownAudio(Buffer.from(message.pcm_b64, 'base64'), downAudioMeta(message));
    } else if (message.type === 'ctrl') {
      this.onCtrl(message.action, message.reason || '');
    }
  }

  sendUpAudio(pcmBuffer) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this._activeHello) return;
    this.ws.send(JSON.stringify({ type: 'up_audio', pcm_b64: pcmBuffer.toString('base64') }));
  }

  sendPeerState(state, userId) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this._activeHello) return;
    this.ws.send(JSON.stringify({ type: 'peer_state', state, user_id: userId }));
  }

  // 上行 ctrl（sidecar → rtc_bridge）。当前唯一动作：note_termination 终止上下文中继。
  sendCtrl(payload) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this._activeHello) {
      return false;
    }
    try {
      this.ws.send(JSON.stringify(payload));
      return true;
    } catch (_) { return false; }
  }

  // 把终止上下文（termination_id）中继给 rtc_bridge，供其 drain 后上报
  // bridge_drained_closed。fail-safe：未连接/参数非法一律返回 false，不抛错，
  // 绝不影响拆链主流程。上游接入点（获知 tid 处）调用本方法即可。
  noteTermination(sessionId, terminationId) {
    if (typeof sessionId !== 'string' || !sessionId) return false;
    if (typeof terminationId !== 'string' || !terminationId) return false;
    if (terminationId.length > 128) return false;
    return this.sendCtrl({
      type: 'ctrl', action: 'note_termination',
      session_id: sessionId, termination_id: terminationId,
    });
  }

  close() {
    this._stop = true;
    this.clearSession();
  }
}

module.exports = { BridgeClient, sessionHello };
