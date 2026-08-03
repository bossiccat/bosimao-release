/**
 * 提醒气泡 — 与 docs/SPEC.md §4.2 / DESIGN.md 四级打扰契约一致
 * - role="alert" + aria-live="assertive"（屏幕阅读器即时播报）
 * - 手动关闭钮 + 可配置自动消失（默认 8s）
 * - 状态点表达语义色（不采用 border-left 彩色条反模式）
 * - 阴影走 --shadow-card token
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

const STATE_TONE: Record<string, string> = {
  stuck: "var(--warn)",
  off_track: "var(--danger)",
};

export function ReminderToast({ alert, onDismiss, autoDismissMs = 8000 }: ReminderToastProps) {
  useEffect(() => {
    const t = setTimeout(onDismiss, autoDismissMs);
    return () => clearTimeout(t);
  }, [onDismiss, autoDismissMs]);

  const tone = STATE_TONE[alert.state] ?? "var(--accent)";
  const label = STATE_LABEL[alert.state] ?? "提醒";

  return (
    <div
      className="reminder-toast"
      role="alert"
      aria-live="assertive"
      aria-label={`${alert.app_id} ${label}提醒`}
    >
      <div className="rt-head">
        <span className="rt-dot" style={{ backgroundColor: tone }} aria-hidden="true" />
        <span className="rt-title">{alert.app_id} · {label}</span>
        <button type="button" className="rt-close" onClick={onDismiss} aria-label="关闭提醒">
          <X size={14} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>
      <div className="rt-body">{alert.summary}</div>
      {alert.suggestion && <div className="rt-sug">{alert.suggestion}</div>}
      <style>{`
        .reminder-toast {
          position: fixed; right: 16px; bottom: 180px; z-index: 30;
          width: 280px;
          background: var(--surface);
          border: 1px solid var(--border);
          border-radius: 12px;
          padding: 12px 14px;
          font-size: 13px;
          box-shadow: var(--shadow-card);
          animation: toast-in 0.25s ease-out;
        }
        .rt-head { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
        .rt-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
        .rt-title {
          font-weight: 590;
          font-family: var(--font-mono);
          font-size: 12px;
          color: var(--fg-2);
        }
        .rt-close {
          margin-left: auto;
          display: inline-flex; align-items: center; justify-content: center;
          width: 22px; height: 22px;
          border: none; border-radius: 6px;
          background: transparent;
          color: var(--muted);
          cursor: pointer;
          transition:
            background-color var(--motion-fast) ease,
            color var(--motion-fast) ease;
        }
        .rt-close:hover { background: var(--surface-2); color: var(--fg); }
        .rt-close:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .rt-body { color: var(--fg); }
        .rt-sug { margin-top: 6px; color: var(--fg-2); font-size: 12px; }
        @keyframes toast-in {
          from { opacity: 0; transform: translateY(8px); }
          to { opacity: 1; transform: translateY(0); }
        }
        @media (prefers-reduced-motion: reduce) {
          .reminder-toast { animation: none; }
        }
      `}</style>
    </div>
  );
}
