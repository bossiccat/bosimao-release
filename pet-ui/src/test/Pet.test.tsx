/**
 * Pet 组件三态契约测试（商业化落地 2026-09-03）
 * 契约：idle/监控态宠物本体清晰可见（opacity 0.9），且必须是波斯猫形象
 * （立耳特征）；提醒态 1.0 + 脉冲。透明是"模式"不是日常态。
 */
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { Pet } from "../components/Pet";

describe("Pet — 商业化三态契约", () => {
  it("监控态：宠物本体默认 opacity 0.9（清晰可辨，不再是 0.3 残影）", () => {
    const { container } = render(<Pet />);
    const root = container.querySelector<HTMLElement>(".pet");
    expect(root).not.toBeNull();
    expect(root!.style.opacity).toBe("0.9");
  });

  it("监控态显式传入 0.9 由 App 控制时同样生效", () => {
    const { container } = render(<Pet mode="monitoring" opacity={0.9} />);
    const root = container.querySelector<HTMLElement>(".pet");
    expect(root!.style.opacity).toBe("0.9");
  });

  it("提醒态：opacity 1.0 全不透明 + 脉冲动画标记", () => {
    const { container } = render(<Pet mode="alerting" alertPulse />);
    const root = container.querySelector<HTMLElement>(".pet");
    expect(root!.style.opacity).toBe("1");
    expect(root!.className).toContain("pet-alert-pulse");
  });

  it("必须是波斯猫：具备立耳特征（左耳/右耳 SVG 组）", () => {
    const { getByTestId } = render(<Pet />);
    expect(getByTestId("pet-ear-left")).toBeInTheDocument();
    expect(getByTestId("pet-ear-right")).toBeInTheDocument();
  });

  it("必须具备面部特征（眼/鼻）与光晕呼吸外壳", () => {
    const { getByTestId } = render(<Pet />);
    expect(getByTestId("pet-face")).toBeInTheDocument();
    expect(getByTestId("pet-shell")).toBeInTheDocument();
  });

  it("tone 语义色映射仍生效：danger 时使用 --danger 变量", () => {
    const { getByTestId } = render(<Pet tone="danger" />);
    const face = getByTestId("pet-face");
    expect(face.getAttribute("data-tone")).toBe("danger");
  });
});
