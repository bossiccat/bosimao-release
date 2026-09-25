/**
 * 宠物「星核 Spark」— 商业插画资产版（2026-09-25 v3）
 * 门面插画走「ImageGen 生成 → 纯白底图生图保角色 → 云端模型抠图 → 多尺寸导出」资产管线
 * （产物与过程证据：outputs/2026-09-25-pet-redo-v3/），组件不再手绘 SVG。
 * 职责：精灵渲染 + 状态效果层（光环 / 思考点 / 律动 / 提醒脉动）+ 地面接触阴影。
 * 颜色全部来自 design-tokens.css；样式进 pet.css，禁止运行时内联 <style>。
 */
import spriteUrl from "../assets/jax_pet_sprite.png";
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

export function Pet({
  mode = "monitoring",
  tone = "neutral",
  state,
  sizePx = 152,
  opacity = 0.9,
  alertPulse = false,
  children,
}: PetProps) {
  const resolvedState: PetState = state ?? (mode === "alerting" ? "alerting" : "idle");
  const alerting = resolvedState === "alerting";
  const resolvedOpacity = alerting ? 1 : opacity;

  return (
    <div
      className={`pet pet-${mode} pet--${resolvedState} ${alertPulse ? "pet-alert-pulse" : ""}`}
      data-state={resolvedState}
      data-tone={tone}
      data-testid="pet-root"
      style={{ width: sizePx, height: sizePx, opacity: resolvedOpacity, position: "relative" }}
    >
      {/* 状态/语义光环：径向柔光（非描边硬环），idle/thinking 不显形 */}
      <span className="pet-halo" data-testid="pet-shell" aria-hidden="true" />
      {/* 接触阴影：让猫"坐"在桌面上 */}
      <span className="pet-shadow" aria-hidden="true" />
      {/* 门面精灵：插画资产（512px 源，2x+ 余量覆盖 176px 使用上限） */}
      <img
        className="pet-sprite"
        data-testid="pet-sprite"
        src={spriteUrl}
        alt="星核"
        draggable={false}
      />
      {/* 思考点（仅 thinking 态显形） */}
      <span className="pet-think-dots" data-testid="pet-think-dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      {children}
    </div>
  );
}
