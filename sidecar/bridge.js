// bridge.js —— sidecar ↔ rtc_bridge localhost WS 客户端（127.0.0.1:19092）
//
// 消息契约（JSON 文本帧；与 backend/rtc_bridge/server.py 对齐）：
//   sidecar→bridge: {type:"hello", role, sdk_version, device_id, room_id, user_id}
//   sidecar→bridge: {type:"up_audio", pcm_b64}          # 手机远端音频（16k s16 mono）
//   sidecar→bridge: {type:"peer_state", state:"enter"|"leave", user_id}
//   bridge→sidecar: {type:"ready"}
//   bridge→sidecar: {type:"down_audio", pcm_b64}        # 回复音频（16k s16 mono）
//   bridge→sidecar: {type:"ctrl", action:"exit", reason}
//
// sidecar 是 WS 客户端（rtc_bridge 是服务端，绑定 127.0.0.1 不对外）。
const log = require('./logger');

class BridgeClient {
  constructor(url, onDownAudio, onCtrl) {
    this.url = url;
    this.onDownAudio = onDownAudio;   // (pcmBuffer) => void
    this.onCtrl = onCtrl;             // (action, reason) => void
    this.ws = null;
    this.connected = false;
    this._stop = false;
    this._backoffIdx = 0;
    this._hello = null;
  }

  start(hello) {
    this._hello = hello;
    this._connect();
  }

  _connect() {
    if (this._stop) return;
    const ws = new WebSocket(this.url);
    this.ws = ws;

    ws.onopen = () => {
      this.connected = true;
      this._backoffIdx = 0;
      log('WS', `已连接 rtc_bridge ${this.url}`);
      if (this._hello) ws.send(JSON.stringify(this._hello));
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (msg.type === 'down_audio' && msg.pcm_b64) {
        const buf = Buffer.from(msg.pcm_b64, 'base64');
        this.onDownAudio(buf);
      } else if (msg.type === 'ctrl') {
        this.onCtrl(msg.action, msg.reason || '');
      } else if (msg.type === 'ready') {
        log('WS', 'rtc_bridge 就绪');
      }
    };

    ws.onclose = () => {
      this.connected = false;
      if (this._stop) return;
      const delay = [1, 2, 4, 8][Math.min(this._backoffIdx, 3)] * 1000;
      this._backoffIdx += 1;
      log('WS', `连接断开，${delay / 1000}s 后重连`);
      setTimeout(() => this._connect(), delay);
    };

    ws.onerror = () => {
      try { ws.close(); } catch (e) { /* ignore */ }
    };
  }

  sendUpAudio(pcmBuffer) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: 'up_audio', pcm_b64: pcmBuffer.toString('base64') }));
  }

  sendPeerState(state, userId) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: 'peer_state', state, user_id: userId }));
  }

  close() {
    this._stop = true;
    try { if (this.ws) this.ws.close(); } catch (e) { /* ignore */ }
  }
}

module.exports = { BridgeClient };
