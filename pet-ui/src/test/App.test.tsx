/**
 * App 三态交互契约测试（商业化落地 2026-09-03）
 * 契约（业界共识调研 2026-09-03）：
 * - idle：仅波斯猫 logo 可见，控件（徽章/设置/隐藏）全部隐藏
 * - hover：控件淡入（data-hidden 翻转）
 * - click logo：展开监控主面板（"点进去画面"）
 * - 面板打开期间控件保持可见（在放大窗口中可操作）
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

// Tauri IPC / 事件 / WS 客户端全部 mock（jsdom 无 Tauri 运行时）
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => false,
  invoke: vi.fn().mockResolvedValue(false),
}));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn().mockResolvedValue(() => {}),
}));
vi.mock("../state/wsClient", () => ({
  wsClient: {
    on: () => () => {},
    onConn: () => () => {},
    connect: () => {},
    close: () => {},
    control: () => {},
    getConnState: () => "open",
  },
}));

import App from "../App";

beforeEach(() => {
  cleanup();
});

describe("App — 三态交互模型", () => {
  it("idle 态：设置/隐藏控件隐藏（data-hidden=true），徽章隐藏", () => {
    render(<App />);
    const settings = screen.getByRole("button", { name: "打开设置" });
    const hide = screen.getByRole("button", { name: "隐藏宠物（可从系统托盘找回）" });
    expect(settings.closest("[data-hidden]")).toHaveAttribute("data-hidden", "true");
    expect(hide.closest("[data-hidden]")).toHaveAttribute("data-hidden", "true");
    const badge = document.querySelector(".conn-badge-slot");
    expect(badge).toHaveAttribute("data-hidden", "true");
  });

  it("hover 态：移入窗口控件淡入（data-hidden=false），移出恢复隐藏", () => {
    const { container } = render(<App />);
    const root = container.querySelector(".app-root") as HTMLElement;
    fireEvent.mouseEnter(root);
    expect(screen.getByRole("button", { name: "打开设置" }).closest("[data-hidden]")).toHaveAttribute(
      "data-hidden",
      "false",
    );
    expect(document.querySelector(".conn-badge-slot")).toHaveAttribute("data-hidden", "false");
    fireEvent.mouseLeave(root);
    expect(screen.getByRole("button", { name: "打开设置" }).closest("[data-hidden]")).toHaveAttribute(
      "data-hidden",
      "true",
    );
  });

  it("click 波斯猫 logo：展开监控主面板（面板打开期间控件保持可见）", () => {
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "打开监控面板" }));
    // 面板出现：MonitorPanel 标题渲染（sessions 为空时显示空态提示）
    expect(screen.getByText("监控面板")).toBeInTheDocument();
    expect(screen.getByText(/暂无监控目标/)).toBeInTheDocument();
    // 面板打开期间控件不隐藏
    expect(screen.getByRole("button", { name: "打开设置" }).closest("[data-hidden]")).toHaveAttribute(
      "data-hidden",
      "false",
    );
  });

  it("idle 态宠物居中（pet-anchor--idle），面板打开时让位右下", () => {
    const { container } = render(<App />);
    const anchor = container.querySelector(".pet-anchor") as HTMLElement;
    expect(anchor.className).toContain("pet-anchor--idle");
    fireEvent.click(anchor);
    expect(anchor.className).not.toContain("pet-anchor--idle");
  });

  it("idle 态宠物本体透明度为 0.9（不再传 0.3 残影值）", () => {
    const { container } = render(<App />);
    const pet = container.querySelector(".pet") as HTMLElement;
    expect(pet.style.opacity).toBe("0.9");
  });
});
