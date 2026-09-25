/**
 * 底部控件条（第5节控件组）
 * - 状态胶囊（连接状态）+ 设置 / 隐藏两个图标按钮，统一收纳进一条有表面的底座，
 *   给状态胶囊与按钮明确的视觉锚点（不再各自漂浮）。
 * - 三态模型由 App 的 controlsHidden 驱动；样式见 styles/shell.css。
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
    <div className="control-dock" data-hidden={controlsHidden}>
      <div className="conn-badge-slot" data-hidden={controlsHidden}>
        <ConnectionBadge voicePhase={voicePhase} />
      </div>

      <button
        type="button"
        className="settings-trigger"
        aria-label="打开设置"
        onClick={onToggleSettings}
      >
        <SettingsIcon size={16} strokeWidth={1.8} aria-hidden="true" />
      </button>

      <button
        type="button"
        className="hide-trigger"
        aria-label="隐藏宠物（可从系统托盘找回）"
        onClick={onHidePet}
      >
        <EyeOff size={16} strokeWidth={1.8} aria-hidden="true" />
      </button>
    </div>
  );
}
