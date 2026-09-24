/**
 * 语音体验光球 — 跨端一致 10 态（Task 8 / SPEC §4.2，与 Android ExperienceState 同名枚举）
 * idle / requesting_permission / connecting / listening / endpointing /
 * thinking / speaking / interrupted / recovering / error
 * Listening(声呐脉冲环，幅度随音量) / Thinking(内部流动加速+靛色) / Speaking(波形涟漪)；
 * error 使用语义色大号展示；其余状态静态呈现。
 */
import { Pet } from "./Pet";
import "../styles/voice-orb.css";

export type VoicePhase =
  | "idle"
  | "requesting_permission"
  | "connecting"
  | "listening"
  | "endpointing"
  | "thinking"
  | "speaking"
  | "interrupted"
  | "recovering"
  | "error";

interface VoiceOrbProps {
  phase: VoicePhase;
  tone?: "neutral" | "warn" | "danger";
  volume?: number; // 0-1，Listening 声呐幅度
}

// 语义色全部走 design-tokens.css 的 C extension 语音状态色（Task 12；P0-2）
const PHASE_COLOR: Record<VoicePhase, string> = {
  idle: "var(--voice-idle)",
  requesting_permission: "var(--warn)",
  connecting: "var(--voice-connecting)",
  listening: "var(--voice-listening)",
  endpointing: "var(--voice-endpointing)",
  thinking: "var(--voice-thinking)",
  speaking: "var(--voice-speaking)",
  interrupted: "var(--voice-interrupted)",
  recovering: "var(--voice-recovering)",
  error: "var(--voice-error)",
};

export function VoiceOrb({ phase, tone = "neutral", volume = 0.5 }: VoiceOrbProps) {
  const color = PHASE_COLOR[phase];
  const isError = phase === "error";

  return (
    <div className={`voice-orb voice-${phase}`}>
      <Pet
        mode={isError ? "alerting" : "monitoring"}
        tone={tone}
        sizePx={isError ? 140 : 96}
        opacity={isError ? 1 : 0.6}
        alertPulse={isError}
      >
        {/* 声呐脉冲环（Listening） */}
        {phase === "listening" && (
          <div
            className="sonar-ring"
            style={{
              width: `${60 + volume * 60}%`,
              height: `${60 + volume * 60}%`,
              borderColor: color,
            }}
          />
        )}
        {/* 波形涟漪（Speaking） */}
        {phase === "speaking" && <div className="speak-ripple" style={{ borderColor: color }} />}
      </Pet>
    </div>
  );
}
