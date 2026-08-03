/**
 * 宠物光球「星核 Spark」— 与 docs/DESIGN.md §3 契约一致
 * - 监控态：64-96px / 20-40% 透明度 / 贴边低幅呼吸
 * - 提醒态：120-160px / 100% / 语义色 2Hz 脉冲
 * - 语音态：由 VoiceOrb 承载（Listening/Thinking/Speaking）
 * - reduced-motion：静态颜色 + 透明度变化
 */
import { useMemo } from "react";

export type PetMode = "monitoring" | "alerting";
export type PetTone = "neutral" | "success" | "warn" | "danger";

interface PetProps {
  mode?: PetMode;
  tone?: PetTone;
  sizePx?: number; // 监控 80 / 提醒 140
  opacity?: number; // 监控 0.3 / 提醒 1.0
  alertPulse?: boolean;
  children?: React.ReactNode; // 状态点等附加元素
}

// 语义色全部走 CSS 变量（P0-2），SVG stopColor 经 style 属性绑定才能解析 var()
const TONE_COLOR: Record<PetTone, string> = {
  neutral: "var(--accent)",
  success: "var(--success)",
  warn: "var(--warn)",
  danger: "var(--danger)",
};

export function Pet({ mode = "monitoring", tone = "neutral", sizePx = 80, opacity = 0.3, alertPulse = false, children }: PetProps) {
  const coreColor = useMemo(() => TONE_COLOR[tone], [tone]);

  // 径向渐变（渲染开销最低，3060 常驻友好）
  const gradientId = `pet-grad-${tone}-${sizePx}`;

  return (
    <div
      className={`pet pet-${mode} ${alertPulse ? "pet-alert-pulse" : ""}`}
      style={{
        width: sizePx,
        height: sizePx,
        opacity,
        position: "relative",
      }}
    >
      <svg viewBox="0 0 100 100" width="100%" height="100%" style={{ overflow: "visible" }}>
        <defs>
          <radialGradient id={gradientId} cx="50%" cy="45%" r="60%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.9" />
            <stop offset="25%" style={{ stopColor: coreColor }} stopOpacity="0.85" />
            <stop offset="70%" style={{ stopColor: coreColor }} stopOpacity="0.35" />
            <stop offset="100%" style={{ stopColor: coreColor }} stopOpacity="0.05" />
          </radialGradient>
        </defs>
        {/* 核心光体 */}
        <circle cx="50" cy="50" r="42" fill={`url(#${gradientId})`} />
        {/* 核心高光（似眼睛） */}
        <ellipse cx="42" cy="42" rx="10" ry="7" fill="#ffffff" opacity="0.6" />
        {/* 柔和能量外壳 */}
        <circle
          cx="50"
          cy="50"
          r="48"
          fill="none"
          style={{ stroke: coreColor }}
          strokeOpacity="0.25"
          strokeWidth="1.5"
          className="pet-shell"
        />
      </svg>
      {children}
      <style>{`
        .pet {
          transition: opacity 0.3s ease;
          cursor: pointer;
        }
        .pet-shell {
          animation: pet-breathe 6s ease-in-out infinite;
          transform-origin: center;
        }
        @keyframes pet-breathe {
          0%, 100% { transform: scale(1); opacity: 0.25; }
          50% { transform: scale(1.04); opacity: 0.4; }
        }
        .pet-alert-pulse {
          animation: pet-alert 0.5s ease-in-out infinite;
        }
        @keyframes pet-alert {
          0%, 100% { transform: scale(1); }
          50% { transform: scale(1.06); }
        }
        @media (prefers-reduced-motion: reduce) {
          .pet-shell { animation: none; }
          .pet-alert-pulse { animation: none; }
        }
      `}</style>
    </div>
  );
}
