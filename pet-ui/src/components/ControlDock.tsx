/**
 * 底部控件条（§5.1）— 状态胶囊 + 设置 / 隐藏两个图标按钮 + 退出（隔离竖线）。
 * 三态由 App 的 controlsHidden 驱动；退出改用瓷面确认（替代原生 window.confirm）。
 */
import { useState } from "react";
import { Power, Settings as SettingsIcon } from "lucide-react";
import { JaxMoonNap } from "./icons";
import { ConnectionBadge, type VoiceConnPhase } from "./ConnectionBadge";

interface ControlDockProps {
  controlsHidden: boolean;
  voicePhase: VoiceConnPhase;
  onToggleSettings: () => void;
  onHidePet: () => void;
  onConfirmQuit: () => void;
}

export function ControlDock({
  controlsHidden,
  voicePhase,
  onToggleSettings,
  onHidePet,
  onConfirmQuit,
}: ControlDockProps) {
  const [confirmingQuit, setConfirmingQuit] = useState(false);

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
        <SettingsIcon size={16} strokeWidth={1.75} aria-hidden="true" />
      </button>

      <button
        type="button"
        className="hide-trigger"
        aria-label="隐藏宠物（可从系统托盘找回）"
        onClick={onHidePet}
      >
        <JaxMoonNap size={16} strokeWidth={1.75} aria-hidden="true" />
      </button>

      <span className="dock-divider" aria-hidden="true" />

      <button
        type="button"
        className="quit-trigger"
        aria-label="退出贾克斯"
        onClick={() => setConfirmingQuit(true)}
      >
        <Power size={16} strokeWidth={1.75} aria-hidden="true" />
      </button>

      {confirmingQuit && (
        <div className="quit-confirm" role="dialog" aria-modal="true" aria-label="退出确认">
          <div className="quit-confirm-card">
            <p className="quit-confirm-text">退出贾克斯？下次可从系统托盘重新打开。</p>
            <div className="quit-confirm-actions">
              <button
                type="button"
                className="quit-confirm-primary"
                onClick={() => {
                  setConfirmingQuit(false);
                  onConfirmQuit();
                }}
              >
                退出
              </button>
              <button
                type="button"
                className="quit-confirm-secondary"
                onClick={() => setConfirmingQuit(false)}
              >
                取消
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
