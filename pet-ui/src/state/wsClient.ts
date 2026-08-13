/**
 * WebSocket 客户端（连接后端 /ws/pet）— 与 docs/openapi.yaml WS 契约一致
 * 心跳 15s / 断线重连（指数退避 1s→30s）
 *
 * 2026-08-13 UI 商业化升级：暴露连接生命周期事件（conn 状态），
 * 供 UI 呈现「连接中 / 已连接 / 重连中」三层状态（AC-20 故障可感知）。
 */
export type WsEvent =
  | { type: "event"; event: string; data: unknown }
  | { type: "pong"; ts: number }
  | { type: "ack"; action: string };

export type WsConnState = "connecting" | "open" | "reconnecting";

type Listener = (evt: WsEvent) => void;
type ConnListener = (state: WsConnState) => void;

const WS_URL = "wss://127.0.0.1:8000/ws/pet";

export class WsClient {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private connListeners = new Set<ConnListener>();
  private retryDelay = 1000;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private closed = false;
  private connState: WsConnState = "connecting";

  connect() {
    this.closed = false;
    this.setConnState("connecting");
    this.open();
  }

  private open() {
    if (this.closed) return;
    this.ws = new WebSocket(WS_URL);

    this.ws.onopen = () => {
      this.retryDelay = 1000;
      this.setConnState("open");
      this.startHeartbeat();
    };

    this.ws.onmessage = (msg) => {
      try {
        const evt = JSON.parse(msg.data) as WsEvent;
        this.listeners.forEach((l) => l(evt));
      } catch {
        /* 忽略非 JSON */
      }
    };

    this.ws.onclose = () => {
      this.stopHeartbeat();
      if (!this.closed) {
        this.setConnState("reconnecting");
        setTimeout(() => this.open(), this.retryDelay);
        this.retryDelay = Math.min(this.retryDelay * 2, 30000); // 指数退避上限 30s
      }
    };

    this.ws.onerror = () => {
      // onerror 后必然 onclose；这里只负责状态提示（避免重复触发）
      this.setConnState("reconnecting");
    };
  }

  on(listener: Listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** UI 订阅连接生命周期状态 */
  onConn(listener: ConnListener) {
    this.connListeners.add(listener);
    listener(this.connState);
    return () => {
      this.connListeners.delete(listener);
    };
  }

  getConnState(): WsConnState {
    return this.connState;
  }

  private setConnState(state: WsConnState) {
    this.connState = state;
    this.connListeners.forEach((l) => l(state));
  }

  send(msg: unknown) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  ping() {
    this.send({ type: "ping", ts: Date.now() });
  }

  control(action: string, target?: string) {
    this.send({ type: "control", action, target });
  }

  close() {
    this.closed = true;
    this.stopHeartbeat();
    this.ws?.close();
  }

  private startHeartbeat() {
    this.stopHeartbeat();
    this.heartbeat = setInterval(() => this.ping(), 15000);
  }

  private stopHeartbeat() {
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.heartbeat = null;
  }
}

export const wsClient = new WsClient();
