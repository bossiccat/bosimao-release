/**
 * 宠物「星核 Spark」— 波斯猫商业插画（2026-09-24 重做）
 * 契约：常态 200×200 透明窗内只有这一只猫 = 产品门面，必须商业插画水准。
 * - viewBox 0 0 240 240；默认渲染 152px（提醒态 176）。
 * - 完整猫：地面阴影 / 尾巴 / 躯干 / 前爪 / 头 / 内外耳 / 眼含高光 / 鼻 / 锥形胡须×6 / 项圈吊牌。
 * - 描边用 --pet-outline，粗细随部位变化（外轮廓 2.5 / 内部结构 1.5），round 连接。
 * - 颜色全部来自 token（#fff/#000 为规范允许例外）；样式进 pet.css，禁止运行时内联 <style>。
 */
import { useId, useMemo } from "react";
import "../styles/pet.css";

export type PetMode = "monitoring" | "alerting";
export type PetTone = "neutral" | "success" | "warn" | "danger";
export type PetState = "idle" | "listening" | "thinking" | "speaking" | "alerting";

interface PetProps {
  mode?: PetMode;
  tone?: PetTone;
  state?: PetState; // 直接指定五态视觉；省略时由 mode 推导（alerting → alerting，否则 idle）
  sizePx?: number; // 常态 152 / 提醒 176
  opacity?: number; // 常态 0.9（清晰可辨）
  alertPulse?: boolean;
  children?: React.ReactNode;
}

const TONE_COLOR: Record<PetTone, string> = {
  neutral: "var(--voice-idle)",
  success: "var(--success)",
  warn: "var(--warn)",
  danger: "var(--danger)",
};

// 锥形胡须：两端收细、中段微弯的曲线（lens 形），区别于等粗直线。
function whisker(x1: number, y1: number, x2: number, y2: number, w: number, bend: number): string {
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  const dx = x2 - x1;
  const dy = y2 - y1;
  const len = Math.hypot(dx, dy) || 1;
  const px = -dy / len;
  const py = dx / len;
  const cx = mx + px * bend;
  const cy = my + py * bend;
  const a1x = cx + px * w;
  const a1y = cy + py * w;
  const a2x = cx - px * w;
  const a2y = cy - py * w;
  const f = (n: number) => n.toFixed(1);
  return `M${f(x1)} ${f(y1)} Q${f(a1x)} ${f(a1y)} ${f(x2)} ${f(y2)} Q${f(a2x)} ${f(a2y)} ${f(x1)} ${f(y1)} Z`;
}

interface EyeProps {
  cx: number;
  cy: number;
  irisR: number;
  pupilRx: number;
  pupilRy: number;
}

// 眼白/虹膜/瞳孔/高光 四层；虹膜+瞳孔随状态缩放（聆听放大 / 提醒收缩）可见
function Eye({ cx, cy, irisR, pupilRx, pupilRy }: EyeProps) {
  return (
    <g className="pet-eye">
      <ellipse cx={cx} cy={cy} rx={15} ry={17} fill="#fff" stroke="var(--pet-outline)" strokeWidth={1.5} />
      <circle cx={cx} cy={cy} r={irisR} fill="var(--pet-eye)" />
      <ellipse cx={cx} cy={cy} rx={pupilRx} ry={pupilRy} fill="#000" opacity={0.9} />
      <circle cx={cx - 4} cy={cy - 6} r={3} fill="#fff" />
    </g>
  );
}

export function Pet({
  mode = "monitoring",
  tone = "neutral",
  state,
  sizePx = 152,
  opacity = 0.9,
  alertPulse = false,
  children,
}: PetProps) {
  const uid = useId().replace(/:/g, "");
  const resolvedState: PetState = state ?? (mode === "alerting" ? "alerting" : "idle");
  const alerting = resolvedState === "alerting";
  const coreColor = useMemo(() => TONE_COLOR[tone], [tone]);
  // 光晕语义色（pet-shell）：提醒态用 tone 语义色、语音态用 --voice-*；
  // idle/thinking 不出现光晕（规格第 4 节：常态桌面只有猫本体）。
  const haloColor = alerting
    ? coreColor
    : resolvedState === "listening"
      ? "var(--voice-listening)"
      : resolvedState === "speaking"
        ? "var(--voice-speaking)"
        : "var(--voice-idle)";
  const resolvedOpacity = alerting ? 1 : opacity;

  // 瞳孔随状态：聆听放大、提醒收缩、其余常态（虹膜同步缩放以便肉眼可辨）。
  const pupil = useMemo(() => {
    if (resolvedState === "listening") return { irisR: 14, rx: 8, ry: 12 };
    if (resolvedState === "alerting") return { irisR: 8.5, rx: 3, ry: 6 };
    return { irisR: 11.5, rx: 5.5, ry: 10 };
  }, [resolvedState]);

  const whiskersL = [
    whisker(108, 118, 40, 106, 3, 7),
    whisker(108, 124, 34, 122, 3, 7),
    whisker(108, 130, 44, 138, 3, 7),
  ];
  const whiskersR = [
    whisker(132, 118, 200, 106, 3, -7),
    whisker(132, 124, 206, 122, 3, -7),
    whisker(132, 130, 196, 138, 3, -7),
  ];

  return (
    <div
      className={`pet pet-${mode} pet--${resolvedState} ${alertPulse ? "pet-alert-pulse" : ""}`}
      data-state={resolvedState}
      style={{ width: sizePx, height: sizePx, opacity: resolvedOpacity, position: "relative" }}
    >
      <svg viewBox="0 0 240 240" width="100%" height="100%" style={{ overflow: "visible" }} aria-hidden="true">
        <defs>
          <radialGradient id={`hf-${uid}`} cx="42%" cy="34%" r="72%">
            <stop offset="0%" style={{ stopColor: "var(--pet-fur-light)" }} />
            <stop offset="55%" style={{ stopColor: "var(--pet-fur-base)" }} />
            <stop offset="100%" style={{ stopColor: "var(--pet-fur-shade)" }} />
          </radialGradient>
          <radialGradient id={`bf-${uid}`} cx="40%" cy="28%" r="82%">
            <stop offset="0%" style={{ stopColor: "var(--pet-fur-light)" }} />
            <stop offset="60%" style={{ stopColor: "var(--pet-fur-base)" }} />
            <stop offset="100%" style={{ stopColor: "var(--pet-fur-shade)" }} />
          </radialGradient>
          <linearGradient id={`tf-${uid}`} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" style={{ stopColor: "var(--pet-fur-base)" }} />
            <stop offset="100%" style={{ stopColor: "var(--pet-fur-deep)" }} />
          </linearGradient>
          {/* 状态光晕（pet-shell）：径向填充 + 淡出到 alpha=0，永不出现闭合描边圆环 */}
          <radialGradient id={`sh-${uid}`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" style={{ stopColor: haloColor, stopOpacity: 0.34 }} />
            <stop offset="62%" style={{ stopColor: haloColor, stopOpacity: 0.12 }} />
            <stop offset="100%" style={{ stopColor: haloColor, stopOpacity: 0 }} />
          </radialGradient>
          {/* 地面接触阴影：深色低 alpha，边缘径向淡出到 0（浅桌面是正常投影，深桌面自然近乎不可见） */}
          <radialGradient id={`sd-${uid}`} cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#000" stopOpacity={0.22} />
            <stop offset="70%" stopColor="#000" stopOpacity={0.06} />
            <stop offset="100%" stopColor="#000" stopOpacity={0} />
          </radialGradient>
        </defs>

        {/* 状态/语义环（halo，置于最底）：径向渐变填充，非描边硬环 */}
        <circle
          data-testid="pet-shell"
          className="pet-shell"
          cx="120"
          cy="120"
          r="116"
          fill={`url(#sh-${uid})`}
        />

        {/* 地面接触阴影：深色低 alpha，边缘淡出 */}
        <ellipse cx="120" cy="220" rx="84" ry="12" fill={`url(#sd-${uid})`} />

        <g className="pet-cat">
          {/* 尾巴（卷曲，置于躯干之后形成前后遮挡） */}
          <g className="pet-tail">
            <path
              d="M150 198 C196 204 236 178 226 142 C220 121 197 112 184 127 C195 133 203 148 197 162 C191 176 174 184 158 192 Z"
              fill={`url(#tf-${uid})`}
              stroke="var(--pet-outline)"
              strokeWidth={2.5}
              strokeLinejoin="round"
            />
          </g>

          {/* 躯干 */}
          <ellipse
            cx="120"
            cy="170"
            rx="56"
            ry="58"
            fill={`url(#bf-${uid})`}
            stroke="var(--pet-outline)"
            strokeWidth={2.5}
          />

          {/* 前爪（含趾间分隔线） */}
          <g className="pet-paws">
            <ellipse cx="99" cy="212" rx="23" ry="15" fill={`url(#bf-${uid})`} stroke="var(--pet-outline)" strokeWidth={2.5} />
            <ellipse cx="141" cy="212" rx="23" ry="15" fill={`url(#bf-${uid})`} stroke="var(--pet-outline)" strokeWidth={2.5} />
            <g stroke="var(--pet-outline)" strokeWidth={1.5} strokeLinecap="round">
              <path d="M92 203 L92 221" fill="none" />
              <path d="M106 203 L106 221" fill="none" />
              <path d="M134 203 L134 221" fill="none" />
              <path d="M148 203 L148 221" fill="none" />
            </g>
          </g>

          {/* 头（圆头 + 腮部空气感，受光/背光双档） */}
          <g className="pet-head" data-testid="pet-face" data-tone={tone}>
            <path
              d="M120 40 C78 40 56 66 56 98 C56 118 70 138 88 146 C104 153 136 153 152 146 C170 138 184 118 184 98 C184 66 162 40 120 40 Z"
              fill={`url(#hf-${uid})`}
              stroke="var(--pet-outline)"
              strokeWidth={2.5}
            />

            {/* 立耳 ×2：外耳 + 内耳（异色） */}
            <g className="pet-ear pet-ear-left" data-testid="pet-ear-left">
              <path d="M64 56 C56 30 64 18 82 30 C90 36 94 46 94 56 Z" fill={`url(#hf-${uid})`} stroke="var(--pet-outline)" strokeWidth={2.5} strokeLinejoin="round" />
              <path d="M70 52 C64 34 70 28 82 36 C88 41 90 48 89 54 Z" fill="var(--pet-inner)" />
            </g>
            <g className="pet-ear pet-ear-right" data-testid="pet-ear-right">
              <path d="M176 56 C184 30 176 18 158 30 C150 36 146 46 146 56 Z" fill={`url(#hf-${uid})`} stroke="var(--pet-outline)" strokeWidth={2.5} strokeLinejoin="round" />
              <path d="M170 52 C176 34 170 28 158 36 C152 41 150 48 151 54 Z" fill="var(--pet-inner)" />
            </g>

            {/* 眼 ×2：眼白/虹膜/瞳孔/高光 */}
            <Eye cx={97} cy={100} irisR={pupil.irisR} pupilRx={pupil.rx} pupilRy={pupil.ry} />
            <Eye cx={143} cy={100} irisR={pupil.irisR} pupilRx={pupil.rx} pupilRy={pupil.ry} />

            {/* 鼻 + 上唇分叉的 y 形嘴 */}
            <path d="M120 115 L129 121 L120 127 L111 121 Z" fill="var(--pet-nose)" stroke="var(--pet-outline)" strokeWidth={1.5} strokeLinejoin="round" />
            <g className="pet-mouth" fill="none" stroke="var(--pet-outline)" strokeWidth={1.8} strokeLinecap="round">
              <path d="M120 127 L120 133" />
              <path d="M120 133 Q111 142 102 137" />
              <path d="M120 133 Q129 142 138 137" />
            </g>

            {/* 胡须 ×6：锥形曲线 */}
            <g className="pet-whiskers" fill="var(--pet-outline)" opacity={0.8}>
              {whiskersL.map((d, i) => (
                <path key={`wl${i}`} d={d} />
              ))}
              {whiskersR.map((d, i) => (
                <path key={`wr${i}`} d={d} />
              ))}
            </g>
          </g>

          {/* 项圈 + 吊牌（品牌绿触点） */}
          <g className="pet-collar" data-testid="pet-collar">
            {/* 项圈呼吸发光（聆听态显形）：贴合的柔光带，非糊脸光斑 */}
            <path className="pet-collar-glow" d="M78 147 Q120 171 162 147 L158 167 Q120 190 82 167 Z" fill="var(--pet-collar)" opacity={0} />
            <path d="M84 150 Q120 168 156 150 L153 162 Q120 180 87 162 Z" fill="var(--pet-collar)" stroke="var(--pet-outline)" strokeWidth={2} strokeLinejoin="round" />
            <circle cx="120" cy="156" r="3.2" fill="none" stroke="var(--pet-outline)" strokeWidth={1.5} />
            <circle cx="120" cy="170" r="9" fill="var(--pet-tag)" stroke="var(--pet-outline)" strokeWidth={1.5} />
            <circle cx="116.5" cy="166.5" r="2.4" fill="#fff" opacity={0.8} />
          </g>
        </g>

        {/* 思考点（仅 thinking 态显形） */}
        <g className="pet-think-dots" fill="var(--pet-outline)">
          <circle cx="104" cy="20" r="4" />
          <circle cx="120" cy="13" r="4.5" />
          <circle cx="136" cy="20" r="4" />
        </g>
      </svg>
      {children}
    </div>
  );
}
