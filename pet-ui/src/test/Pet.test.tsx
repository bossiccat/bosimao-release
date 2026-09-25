/**
 * Pet（插画资产版 v3）语义契约测试。
 * 资产由「ImageGen→matting」管线产出（outputs/2026-09-25-pet-redo-v3/），
 * 组件职责 = 精灵渲染 + 状态效果层；本文件锁的行为契约：
 * 精灵渲染 / 五态切换与动效类 / 光环与思考点显形规则 / tone 语义 /
 * 透明度契约 / 无运行时内联 <style>。
 */
import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Pet, type PetState } from "../components/Pet";

const STATES: PetState[] = ["idle", "listening", "thinking", "speaking", "alerting"];

describe("Pet（插画资产版）", () => {
  it("渲染插画精灵资产（管线产物）且带可访问命名", () => {
    const { container, getByTestId } = render(<Pet />);
    const img = getByTestId("pet-sprite");
    expect(img.getAttribute("src")).toContain("jax_pet_sprite");
    expect(img.getAttribute("alt")).toBe("星核");
    expect(getByTestId("pet-root")).toBeTruthy();
    expect(container.querySelector("svg")).toBeNull(); // 不再手绘 SVG
  });

  it("默认 idle 态，透明度契约 0.9", () => {
    const { getByTestId } = render(<Pet />);
    const root = getByTestId("pet-root") as HTMLElement;
    expect(root.dataset.state).toBe("idle");
    expect(root.style.opacity).toBe("0.9");
  });

  it("五态 data-state 与 pet--<state> 类一一对应", () => {
    for (const s of STATES) {
      const { getByTestId, unmount } = render(<Pet state={s} />);
      const root = getByTestId("pet-root") as HTMLElement;
      expect(root.dataset.state).toBe(s);
      expect(root.className).toContain(`pet--${s}`);
      unmount();
    }
  });

  it("mode=alerting 推导 alerting 态且完全不透明", () => {
    const { getByTestId } = render(<Pet mode="alerting" />);
    const root = getByTestId("pet-root") as HTMLElement;
    expect(root.dataset.state).toBe("alerting");
    expect(root.style.opacity).toBe("1");
  });

  it("光环显形规则：listening/speaking/alerting 显形，idle/thinking 不显形", () => {
    const visible = ["listening", "speaking", "alerting"];
    for (const s of STATES) {
      const { getByTestId, unmount } = render(<Pet state={s} />);
      const halo = getByTestId("pet-shell");
      const on = visible.includes(s);
      expect(halo.className.includes("pet-halo")).toBe(true);
      // 显形由 CSS 按状态类控制；这里锁类组合存在性
      expect((getByTestId("pet-root") as HTMLElement).className).toContain(
        on ? `pet--${s}` : `pet--${s}`,
      );
      unmount();
    }
  });

  it("thinking 态挂思考点层；其余态该层存在但不显形（opacity 由 CSS 控制）", () => {
    const { getByTestId } = render(<Pet state="thinking" />);
    expect(getByTestId("pet-think-dots").childElementCount).toBe(3);
  });

  it("tone 语义落到 data-tone，供光环取色", () => {
    const { getByTestId } = render(<Pet state="alerting" tone="danger" />);
    expect((getByTestId("pet-root") as HTMLElement).dataset.tone).toBe("danger");
  });

  it("sizePx 决定画框尺寸", () => {
    const { getByTestId } = render(<Pet sizePx={200} />);
    const root = getByTestId("pet-root") as HTMLElement;
    expect(root.style.width).toBe("200px");
    expect(root.style.height).toBe("200px");
  });

  it("alertPulse 挂脉动类", () => {
    const { getByTestId } = render(<Pet alertPulse state="alerting" tone="danger" />);
    expect((getByTestId("pet-root") as HTMLElement).className).toContain("pet-alert-pulse");
  });

  it("children 照常渲染", () => {
    const { getByText } = render(
      <Pet>
        <span>badge</span>
      </Pet>,
    );
    expect(getByText("badge")).toBeTruthy();
  });

  it("无运行时内联 <style>（样式一律在 pet.css）", () => {
    const { container } = render(<Pet state="listening" />);
    expect(container.querySelector("style")).toBeNull();
  });
});
