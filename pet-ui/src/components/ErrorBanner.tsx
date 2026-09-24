/**
 * 故障提示条 — AC-20：2 秒内显示分类原因 + 可执行恢复动作
 *
 * 分类：ws（控制面断开）/ voice（语音故障）/ capture（捕获异常）
 * 第5节重做：浅红底（--danger 约 8%）+ 1px 描边 + 图标 + 主/次动作。
 * 样式见 styles/shell.css，颜色只来自 token（浅红底用 color-mix 取 --danger 8%）。
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
      <Icon className="err-icon" size={16} strokeWidth={2} aria-hidden="true" />
      <div className="err-text">
        <span className="err-title">{meta.title}</span>
        <span className="err-reason">{fault.reason}</span>
      </div>
      <div className="err-actions">
        <button type="button" className="err-action" onClick={() => onAction(fault.action)}>
          {fault.actionLabel}
        </button>
        <button type="button" className="err-dismiss" onClick={onDismiss} aria-label="关闭提示">
          <X size={13} strokeWidth={2} aria-hidden="true" />
          <span>关闭</span>
        </button>
      </div>
    </div>
  );
}
