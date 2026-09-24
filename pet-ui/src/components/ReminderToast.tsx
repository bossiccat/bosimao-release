/**
 * 提醒气泡 — 与 docs/SPEC.md §4.2 / DESIGN.md 四级打扰契约一致
 * - role="alert" + aria-live="assertive"（屏幕阅读器即时播报）
 * - 手动关闭钮 + 可配置自动消失（默认 8s）
 * - 状态点表达语义色（不采用 border-left 彩色条反模式）
 * - 阴影走 --elev-raised token（Task 12 design-tokens）
 * 第5节规格：白底卡片 + --elev-raised。样式见 styles/shell.css。
 */
import { useEffect } from "react";
import { X } from "lucide-react";

export interface AlertData {
  app_id: string;
  level: number;
  state: string;
  summary: string;
  suggestion?: string;
}

interface ReminderToastProps {
  alert: AlertData;
  onDismiss: () => void;
  /** 自动消失毫秒数，默认 8000 */
  autoDismissMs?: number;
}

const STATE_LABEL: Record<string, string> = {
  stuck: "卡住",
  off_track: "跑偏",
};

const STATE_TONE: Record<string, "stuck" | "off_track" | "neutral"> = {
  stuck: "stuck",
  off_track: "off_track",
};

export function ReminderToast({ alert, onDismiss, autoDismissMs = 8000 }: ReminderToastProps) {
  useEffect(() => {
    const t = setTimeout(onDismiss, autoDismissMs);
    return () => clearTimeout(t);
  }, [onDismiss, autoDismissMs]);

  const tone = STATE_TONE[alert.state] ?? "neutral";
  const label = STATE_LABEL[alert.state] ?? "提醒";

  return (
    <div
      className="reminder-toast"
      role="alert"
      aria-live="assertive"
      aria-label={`${alert.app_id} ${label}提醒`}
    >
      <div className="rt-head">
        <span className="rt-dot" data-tone={tone} aria-hidden="true" />
        <span className="rt-title">{alert.app_id} · {label}</span>
        <button type="button" className="rt-close" onClick={onDismiss} aria-label="关闭提醒">
          <X size={14} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>
      <div className="rt-body">{alert.summary}</div>
      {alert.suggestion && <div className="rt-sug">{alert.suggestion}</div>}
    </div>
  );
}
