'use strict';

const log = require('./logger');

function sessionHello(session) {
  const required = ['session_id', 'device_id', 'room_id', 'user_id', 'sdk_version'];
  if (!session || required.some((key) => typeof session[key] !== 'string' || !session[key])) {
    throw new Error('SIDECAR_INVALID_SESSION_HELLO');
  }
  return { type: 'hello', role: 'sidecar', ...session };
}

class BridgeClient {
  constructor(url, onDownAudio, onCtrl) {
    this.url = url;
    this.onDownAudio = onDownAudio;
    this.onCtrl = onCtrl;
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
      if (this._stop || !this._activeHello) return;
      const delay = [1, 2, 4, 8][Math.min(this._backoffIdx, 3)] * 1000;
      this._backoffIdx += 1;
      setTimeout(() => this._connect(generation), delay);
    };
    ws.onerror = () => { try { ws.close(); } catch (_) { /* closed */ } };
  }

  _onMessage(event) {
    if (typeof event.data !== 'string') return;
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === 'down_audio' && message.pcm_b64) {
      this.onDownAudio(Buffer.from(message.pcm_b64, 'base64'));
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

  close() {
    this._stop = true;
    this.clearSession();
  }
}

module.exports = { BridgeClient, sessionHello };
