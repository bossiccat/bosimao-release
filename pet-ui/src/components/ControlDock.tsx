/**
 * 控件浮层（第5节控件组）
 * - 设置、隐藏两个图标按钮
 * - 连接状态胶囊
 * 三态模型由 App 的 controlsHidden 驱动；样式见 styles/shell.css。
 */
import { EyeOff, Settings as SettingsIcon } from "lucide-react";
import { ConnectionBadge, type VoiceConnPhase } from "./ConnectionBadge";

interface ControlDockProps {
  controlsHidden: boolean;
  voicePhase: VoiceConnPhase;
  onToggleSettings: () => void;
  onHidePet: () => void;
}

export function ControlDock({
  controlsHidden,
  voicePhase,
  onToggleSettings,
  onHidePet,
}: ControlDockProps) {
  return (
    <>
      <button
        type="button"
        className="settings-trigger"
        data-hidden={controlsHidden}
        aria-label="打开设置"
        onClick={onToggleSettings}
      >
        <SettingsIcon size={16} strokeWidth={1.8} aria-hidden="true" />
      </button>

      <button
        type="button"
        className="hide-trigger"
        data-hidden={controlsHidden}
        aria-label="隐藏宠物（可从系统托盘找回）"
        onClick={onHidePet}
      >
        <EyeOff size={16} strokeWidth={1.8} aria-hidden="true" />
      </button>

      <div className="conn-badge-slot" data-hidden={controlsHidden}>
        <ConnectionBadge voicePhase={voicePhase} />
      </div>
    </>
  );
}
