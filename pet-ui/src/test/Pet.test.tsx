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

  it("重做后仍是完整猫：五态 class / data-state 挂上，默认 idle", () => {
    const { container } = render(<Pet />);
    const root = container.querySelector<HTMLElement>(".pet");
    expect(root!.className).toContain("pet--idle");
    expect(root!.getAttribute("data-state")).toBe("idle");
  });

  it("完整猫图层齐全：地面阴影/尾巴/躯干/前爪/头/内外耳/眼/鼻/项圈均在 DOM", () => {
    const { container } = render(<Pet />);
    // 头（含面部）、左右耳、状态环、项圈 均存在
    expect(container.querySelector('[data-testid="pet-face"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="pet-ear-left"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="pet-ear-right"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="pet-collar"]')).not.toBeNull();
    // 内耳（异色）、鼻、项圈吊牌 应有对应 fill 的 path/circle
    const svg = container.querySelector("svg")!;
    const fills = Array.from(svg.querySelectorAll<SVGElement>("[fill]")).map((e) =>
      (e.getAttribute("fill") || "").replace(/\s/g, "")
    );
    expect(fills.some((f) => f === "var(--pet-nose)")).toBe(true);
    expect(fills.some((f) => f === "var(--pet-inner)")).toBe(true);
    expect(fills.some((f) => f === "var(--pet-tag)")).toBe(true);
    // 尾巴组 + 前爪组
    expect(container.querySelector(".pet-tail")).not.toBeNull();
    expect(container.querySelector(".pet-paws")).not.toBeNull();
  });

  it("锥形胡须共 6 根（每侧 3），区别于等粗直线", () => {
    const { container } = render(<Pet />);
    const whiskers = container.querySelectorAll(".pet-whiskers > path");
    expect(whiskers.length).toBe(6);
  });

  it("禁止运行时内联 <style>：组件不再注入 inline style 标签", () => {
    const { container } = render(<Pet />);
    expect(container.querySelector("style")).toBeNull();
  });

  it("五态视觉可切换：state 直接指定时 data-state 随之变化", () => {
    for (const s of ["idle", "listening", "thinking", "speaking", "alerting"] as const) {
      const { container } = render(<Pet state={s} />);
      expect(container.querySelector<HTMLElement>(".pet")!.getAttribute("data-state")).toBe(s);
      expect(container.querySelector<HTMLElement>(".pet")!.className).toContain(`pet--${s}`);
    }
  });
});
