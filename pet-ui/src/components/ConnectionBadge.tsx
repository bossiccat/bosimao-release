/**
 * 连接状态徽章 — 状态胶囊（§5.1 / §5.8）
 *
 * 胶囊 = 控制面小点（ws）+ 语音状态点（voice 色 + aura/blink）+ 声波条（speaking）+ 文字。
 * 状态呈现不依赖颜色对比：点色 + 文字标签（可访问性契约 §7）。
 * 语音四态视觉：listening/connecting/alerting → aura 呼吸；thinking → 三闪；speaking → 声波条。
 */
import { useEffect, useState } from "react";
import { wsClient, type WsConnState } from "../state/wsClient";
import type { PetState } from "../state/petMachine";

export type VoiceConnPhase =
  | "idle"
  | "connecting"
  | "listening"
  | "thinking"
  | "speaking"
  | "alerting"
  | "error"
  | "recovering";

interface VoiceMeta {
  label: string;
}

function voiceMeta(phase: VoiceConnPhase): VoiceMeta {
  switch (phase) {
    case "connecting":
      return { label: "语音连接中" };
    case "listening":
      return { label: "聆听中" };
    case "thinking":
      return { label: "思考中" };
    case "speaking":
      return { label: "说话中" };
    case "alerting":
      return { label: "需要注意" };
    case "error":
      return { label: "语音故障" };
    case "recovering":
      return { label: "恢复中" };
    default:
      return { label: "空闲" };
  }
}

/** 由语音体验态映射到连接阶段（§5.8 四态 + 连接/恢复/故障） */
export function toVoicePhase(state: PetState): VoiceConnPhase {
  switch (state) {
    case "connecting":
      return "connecting";
    case "listening":
    case "endpointing":
    case "interrupted":
      return "listening";
    case "thinking":
      return "thinking";
    case "speaking":
      return "speaking";
    case "recovering":
      return "recovering";
    case "error":
      return "error";
    default:
      return "idle";
  }
}

function wsMeta(state: WsConnState) {
  switch (state) {
    case "open":
      return { tone: "ok" as const, label: "已连接" };
    case "reconnecting":
      return { tone: "warn" as const, label: "重连中" };
    default:
      return { tone: "neutral" as const, label: "连接中" };
  }
}

export function ConnectionBadge({ voicePhase }: { voicePhase: VoiceConnPhase }) {
  const [ws, setWs] = useState<WsConnState>(() => wsClient.getConnState());
  useEffect(() => wsClient.onConn(setWs), []);

  const v = voiceMeta(voicePhase);
  const w = wsMeta(ws);

  return (
    <div className="conn-badge" role="status" aria-live="polite" data-phase={voicePhase}>
      <span className="conn-ws-dot" data-tone={w.tone} title={`控制面：${w.label}`} />
      <span className="conn-dot" />
      {voicePhase === "speaking" && (
        <span className="conn-bars" aria-hidden="true">
          <i />
          <i />
          <i />
          <i />
        </span>
      )}
      {voicePhase !== "idle" && <span className="conn-label">{v.label}</span>}
    </div>
  );
}
