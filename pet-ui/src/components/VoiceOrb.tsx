/**
 * 语音六态光球 — 与 docs/DESIGN.md §4 六态状态机契约一致
 * Idle(呼吸) / Listening(声呐脉冲环，幅度随音量) / Thinking(内部流动加速+靛色)
 * Speaking(波形涟漪) / Alerting(语义色 2Hz 脉冲) / Monitoring(低频扫描)
 */
import { Pet } from "./Pet";

export type VoicePhase = "idle" | "monitoring" | "listening" | "thinking" | "speaking" | "alerting";

interface VoiceOrbProps {
  phase: VoicePhase;
  tone?: "neutral" | "warn" | "danger";
  volume?: number; // 0-1，Listening 声呐幅度
}

// 语义色全部走 CSS 变量（P0-2）；thinking 用 --thinking（Indigo 纯色，允许）
const PHASE_COLOR: Record<VoicePhase, string> = {
  idle: "var(--accent)",
  monitoring: "var(--accent)",
  listening: "var(--accent)",
  thinking: "var(--thinking)", // Indigo 纯色（P0-2 允许）
  speaking: "var(--accent)",
  alerting: "var(--danger)",
};

export function VoiceOrb({ phase, tone = "neutral", volume = 0.5 }: VoiceOrbProps) {
  const color = PHASE_COLOR[phase];
  const isAlert = phase === "alerting";

  return (
    <div className={`voice-orb voice-${phase}`}>
      <Pet
        mode={isAlert ? "alerting" : "monitoring"}
        tone={tone}
        sizePx={isAlert ? 140 : 96}
        opacity={isAlert ? 1 : 0.6}
        alertPulse={isAlert}
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
      <style>{`
        .voice-orb { position: relative; display: inline-flex; align-items: center; justify-content: center; }
        .sonar-ring {
          position: absolute;
          border: 2px solid;
          border-radius: 50%;
          opacity: 0.7;
          animation: sonar 0.8s ease-out infinite;
          pointer-events: none;
        }
        @keyframes sonar {
          0% { transform: scale(0.8); opacity: 0.7; }
          100% { transform: scale(1.6); opacity: 0; }
        }
        .speak-ripple {
          position: absolute;
          inset: -6%;
          border: 1.5px solid;
          border-radius: 50%;
          opacity: 0.5;
          animation: ripple 1.2s ease-in-out infinite;
          pointer-events: none;
        }
        @keyframes ripple {
          0%, 100% { transform: scale(0.98); opacity: 0.5; }
          50% { transform: scale(1.04); opacity: 0.15; }
        }
        @media (prefers-reduced-motion: reduce) {
          .sonar-ring, .speak-ripple { animation: none; }
        }
      `}</style>
    </div>
  );
}
