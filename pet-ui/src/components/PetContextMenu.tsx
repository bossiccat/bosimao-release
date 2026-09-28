/**
 * 宠物右键菜单（商业化 2026-09-28，用户实测暴露 P1：退出入口不可发现）。
 * 在宠物上右键弹出：隐藏宠物 / 设置 / 退出贾克斯。样式见 shell.css .pet-ctx-*。
 */
import { Power, Settings as SettingsIcon } from "lucide-react";
import { JaxMoonNap } from "./icons";

export interface PetContextMenuProps {
  x: number;
  y: number;
  onHide: () => void;
  onSettings: () => void;
  onQuit: () => void;
  onClose: () => void;
}

export function PetContextMenu({
  x,
  y,
  onHide,
  onSettings,
  onQuit,
  onClose,
}: PetContextMenuProps) {
  const act = (fn: () => void) => () => {
    onClose();
    fn();
  };

  return (
    <>
      {/* 点击菜单外任意处关闭 */}
      <div className="pet-ctx-backdrop" onClick={onClose} onContextMenu={(e) => { e.preventDefault(); onClose(); }} />
      <div
        className="pet-ctx-menu"
        role="menu"
        aria-label="宠物菜单"
        style={{ left: x, top: y }}
      >
        <button type="button" className="pet-ctx-item" role="menuitem" onClick={act(onHide)}>
          <JaxMoonNap size={16} strokeWidth={1.75} aria-hidden="true" />
          <span>隐藏宠物</span>
        </button>
        <button type="button" className="pet-ctx-item" role="menuitem" onClick={act(onSettings)}>
          <SettingsIcon size={16} strokeWidth={1.75} aria-hidden="true" />
          <span>设置</span>
        </button>
        <div className="pet-ctx-sep" aria-hidden="true" />
        <button type="button" className="pet-ctx-item pet-ctx-item--danger" role="menuitem" onClick={act(onQuit)}>
          <Power size={16} strokeWidth={1.75} aria-hidden="true" />
          <span>退出贾克斯</span>
        </button>
      </div>
    </>
  );
}
