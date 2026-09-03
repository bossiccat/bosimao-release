/**
 * 宠物「星核 Spark」— 波斯猫形象（商业化落地 2026-09-03）
 * 契约（业界共识调研：日常态主体必须清晰可辨）：
 * - 监控态：opacity 0.9（清晰可见），波斯猫本体 + 光晕呼吸
 * - 提醒态：1.0 + 语义色 2Hz 脉冲
 * - 语音态：由 VoiceOrb 承载（Listening/Thinking/Speaking）
 * - reduced-motion：静态颜色 + 透明度变化
 * - 颜色全部走 CSS 变量（P0-2 token 化，#ffffff 为规范允许例外）
 */
import { useMemo } from "react";

export type PetMode = "monitoring" | "alerting";
export type PetTone = "neutral" | "success" | "warn" | "danger";

interface PetProps {
  mode?: PetMode;
  tone?: PetTone;
  sizePx?: number; // 监控 80 / 提醒 140
  opacity?: number; // 监控 0.9（2026-09-03 由 0.3 升级：0.3 在浅色桌面近不可见）
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

export function Pet({ mode = "monitoring", tone = "neutral", sizePx = 80, opacity = 0.9, alertPulse = false, children }: PetProps) {
  const coreColor = useMemo(() => TONE_COLOR[tone], [tone]);
  const gradientId = `pet-grad-${tone}-${sizePx}`;
  const alerting = mode === "alerting";
  // 提醒态契约内聚：1.0 全不透明（不受外部 opacity 传参影响）
  const resolvedOpacity = alerting ? 1 : opacity;

  return (
    <div
      className={`pet pet-${mode} ${alertPulse ? "pet-alert-pulse" : ""}`}
      style={{
        width: sizePx,
        height: sizePx,
        opacity: resolvedOpacity,
        position: "relative",
      }}
    >
      <svg viewBox="0 0 100 100" width="100%" height="100%" style={{ overflow: "visible" }} aria-hidden="true">
        <defs>
          <radialGradient id={gradientId} cx="50%" cy="45%" r="60%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.9" />
            <stop offset="25%" style={{ stopColor: coreColor }} stopOpacity="0.85" />
            <stop offset="70%" style={{ stopColor: coreColor }} stopOpacity="0.35" />
            <stop offset="100%" style={{ stopColor: coreColor }} stopOpacity="0.05" />
          </radialGradient>
        </defs>
        {/* 光晕呼吸外壳（保留：提供"活着"的最低能量感） */}
        <circle
          data-testid="pet-shell"
          cx="50"
          cy="50"
          r="48"
          fill="none"
          style={{ stroke: coreColor }}
          strokeOpacity="0.3"
          strokeWidth="1.5"
          className="pet-shell"
        />
        {/* 波斯猫本体（商业化 2026-09-03：替换无特征光球） */}
        <g data-testid="pet-face" data-tone={tone}>
          {/* 光晕底（渐变能量场，衬托白猫） */}
          <circle cx="50" cy="52" r="40" fill={`url(#${gradientId})`} opacity={alerting ? 0.55 : 0.3} />
          {/* 立耳（外）— 波斯猫识别特征 */}
          <g data-testid="pet-ear-left">
            <path d="M31 46 L25 22 L44 33 Z" fill="#ffffff" style={{ stroke: coreColor }} strokeWidth="2" strokeLinejoin="round" />
            <path d="M33.5 40 L30.5 29 L40 35 Z" style={{ fill: coreColor }} opacity="0.35" />
          </g>
          <g data-testid="pet-ear-right">
            <path d="M69 46 L75 22 L56 33 Z" fill="#ffffff" style={{ stroke: coreColor }} strokeWidth="2" strokeLinejoin="round" />
            <path d="M66.5 40 L69.5 29 L60 35 Z" style={{ fill: coreColor }} opacity="0.35" />
          </g>
          {/* 脸（白） */}
          <ellipse cx="50" cy="55" rx="27" ry="24" fill="#ffffff" style={{ stroke: coreColor }} strokeWidth="2" />
          {/* 扁平面部中分线（波斯猫特征） */}
          <path d="M50 50 L50 60" style={{ stroke: coreColor }} strokeOpacity="0.25" strokeWidth="1.5" strokeLinecap="round" />
          {/* 眼睛（提醒态睁大） */}
          <circle cx="40" cy="53" r={alerting ? 4.5 : 3.5} fill="var(--fg)" />
          <circle cx="60" cy="53" r={alerting ? 4.5 : 3.5} fill="var(--fg)" />
          <circle cx={alerting ? 41.5 : 41} cy={alerting ? 51.5 : 52} r="1.2" fill="#ffffff" />
          <circle cx={alerting ? 61.5 : 61} cy={alerting ? 51.5 : 52} r="1.2" fill="#ffffff" />
          {/* 鼻（语义色小三角） */}
          <path d="M47 62 L53 62 L50 66 Z" style={{ fill: coreColor }} />
          {/* 嘴（w 形） */}
          <path
            d="M44 69 Q47 72 50 69 Q53 72 56 69"
            fill="none"
            style={{ stroke: coreColor }}
            strokeOpacity="0.6"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
          {/* 胡须 */}
          <g style={{ stroke: coreColor }} strokeOpacity="0.35" strokeWidth="1" strokeLinecap="round">
            <path d="M30 60 L18 57" fill="none" />
            <path d="M30 64 L18 65" fill="none" />
            <path d="M70 60 L82 57" fill="none" />
            <path d="M70 64 L82 65" fill="none" />
          </g>
        </g>
      </svg>
      {children}
      <style>{`
        .pet {
          transition: opacity 0.3s ease;
          cursor: pointer;
          filter: drop-shadow(0 2px 6px rgba(0, 0, 0, 0.18));
        }
        .pet-shell {
          animation: pet-breathe 6s ease-in-out infinite;
          transform-origin: center;
        }
        @keyframes pet-breathe {
          0%, 100% { transform: scale(1); opacity: 0.3; }
          50% { transform: scale(1.04); opacity: 0.45; }
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
