/**
 * 故障提示条 — AC-20：2 秒内显示分类原因 + 可执行恢复动作
 *
 * 分类：ws（控制面断开）/ voice（语音故障）/ capture（捕获异常）
 * 恢复动作按钮：重连控制面 / 重启语音 / 打开设置
 */
import { CircleAlert, WifiOff, X } from "lucide-react";

export type FaultCategory = "ws" | "voice" | "capture";

export interface Fault {
  category: FaultCategory;
  reason: string;
  actionLabel: string;
  action: "reconnect" | "restart-voice" | "open-settings";
}

const FAULT_META: Record<FaultCategory, { icon: typeof WifiOff; title: string }> = {
  ws: { icon: WifiOff, title: "控制面连接中断" },
  voice: { icon: CircleAlert, title: "语音链路故障" },
  capture: { icon: CircleAlert, title: "捕获异常" },
};

export function ErrorBanner({
  fault,
  onAction,
  onDismiss,
}: {
  fault: Fault;
  onAction: (action: Fault["action"]) => void;
  onDismiss: () => void;
}) {
  const meta = FAULT_META[fault.category];
  const Icon = meta.icon;

  return (
    <div className="err-banner" role="alert">
      <Icon size={15} strokeWidth={2.2} aria-hidden="true" />
      <div className="err-text">
        <span className="err-title">{meta.title}</span>
        <span className="err-reason">{fault.reason}</span>
      </div>
      <button type="button" className="err-action" onClick={() => onAction(fault.action)}>
        {fault.actionLabel}
      </button>
      <button
        type="button"
        className="err-dismiss"
        aria-label="关闭提示"
        onClick={onDismiss}
      >
        <X size={13} strokeWidth={2.2} aria-hidden="true" />
      </button>
      <style>{`
        .err-banner {
          position: fixed; left: 50%; bottom: 96px; transform: translateX(-50%);
          z-index: 60;
          display: flex; align-items: center; gap: 10px;
          background: var(--surface-2);
          border: 1px solid var(--danger);
          border-radius: 10px;
          padding: 8px 10px 8px 12px;
          max-width: 420px;
          box-shadow: var(--shadow-pop);
          color: var(--fg);
        }
        .err-text { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
        .err-title { font-size: 12px; font-weight: 600; color: var(--danger); }
        .err-reason { font-size: 12px; color: var(--fg-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .err-action {
          display: inline-flex; align-items: center; gap: 5px;
          min-height: 28px; padding: 0 10px;
          background: var(--accent); color: #06121a;
          border: none; border-radius: 6px;
          font-size: 12px; font-weight: 600; cursor: pointer;
          white-space: nowrap;
        }
        .err-action:hover { filter: brightness(1.08); }
        .err-action:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .err-dismiss {
          display: inline-flex; align-items: center; justify-content: center;
          width: 28px; height: 28px;
          background: transparent; border: none; border-radius: 6px;
          color: var(--fg-2); cursor: pointer;
        }
        .err-dismiss:hover { background: var(--surface); color: var(--fg); }
        .err-dismiss:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
      `}</style>
    </div>
  );
}
