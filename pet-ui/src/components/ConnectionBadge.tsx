/**
 * 连接状态徽章 — 双层状态（2026-08-13 UI 商业化升级，AC-20）
 *
 * 上层：WS 控制面连接（connecting / open / reconnecting）
 * 下层：语音全双工会话体验态（idle / connecting / listening / thinking /
 *       speaking / recovering / error）
 *
 * 状态呈现不依赖颜色：图标 + 文字标签（可访问性契约 §7）。
 * 第5节规格：单行状态胶囊，图标 16px，高 32px。样式见 styles/shell.css。
 */
import { useEffect, useState } from "react";
import {
  Wifi,
  WifiOff,
  LoaderCircle,
  AudioLines,
  CircleAlert,
  RefreshCw,
  PowerOff,
  type LucideIcon,
} from "lucide-react";
import { wsClient, type WsConnState } from "../state/wsClient";
import type { PetState } from "../state/petMachine";

export type VoiceConnPhase =
  | "idle"
  | "connecting"
  | "session"
  | "error"
  | "recovering";

interface VoiceMeta {
  icon: LucideIcon;
  label: string;
  tone: "neutral" | "active" | "danger" | "warn";
}

function voiceMeta(phase: VoiceConnPhase): VoiceMeta {
  switch (phase) {
    case "connecting":
      return { icon: LoaderCircle, label: "语音连接中", tone: "active" };
    case "session":
      return { icon: AudioLines, label: "语音会话中", tone: "active" };
    case "error":
      return { icon: CircleAlert, label: "语音故障", tone: "danger" };
    case "recovering":
      return { icon: RefreshCw, label: "恢复中", tone: "warn" };
    default:
      return { icon: PowerOff, label: "语音待机", tone: "neutral" };
  }
}

/** 由语音体验态映射到连接阶段（idle/connecting 之外的会话态归为 session） */
export function toVoicePhase(state: PetState): VoiceConnPhase {
  switch (state) {
    case "connecting":
      return "connecting";
    case "recovering":
      return "recovering";
    case "error":
      return "error";
    case "idle":
      return "idle";
    default:
      return "session"; // listening/endpointing/thinking/speaking/interrupted
  }
}

function wsMeta(state: WsConnState) {
  switch (state) {
    case "open":
      return { icon: Wifi, label: "已连接", tone: "ok" as const };
    case "reconnecting":
      return { icon: WifiOff, label: "重连中", tone: "warn" as const };
    default:
      return { icon: LoaderCircle, label: "连接中", tone: "neutral" as const };
  }
}

export function ConnectionBadge({
  voicePhase,
}: {
  voicePhase: VoiceConnPhase;
}) {
  const [ws, setWs] = useState<WsConnState>(() => wsClient.getConnState());

  useEffect(() => wsClient.onConn(setWs), []);

  const v = voiceMeta(voicePhase);
  const w = wsMeta(ws);
  const VoiceIcon = v.icon;
  const WsIcon = w.icon;

  return (
    <div className="conn-badge" role="status" aria-live="polite">
      <span className={`conn-ws tone-${w.tone}`} title={`控制面：${w.label}`}>
        <WsIcon size={16} strokeWidth={2} aria-hidden="true" />
        {w.label}
      </span>
      <span className={`conn-voice tone-${v.tone}`} title={`语音：${v.label}`}>
        <VoiceIcon size={16} strokeWidth={2} aria-hidden="true" />
        {v.label}
      </span>
    </div>
  );
}
