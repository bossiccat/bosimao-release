/**
 * 监控面板（商业化第三版）
 * - 紧凑标题行 + 分组卡片 + 行式布局
 * - 状态徽章替代原始 ops 数据列，主界面只保留用户可读摘要与元信息
 */
import { useMemo } from "react";
import { Activity, AlertTriangle, CheckCircle2, Code2, TerminalSquare, Braces, X, XCircle } from "lucide-react";
import "../styles/panels.css";

export interface SessionData {
  app_id: string;
  app_name: string;
  window_found: boolean;
  capture_mode: string;
  state: "progress" | "stuck" | "off_track" | "unknown" | "offline";
  state_changed_at: number;
  stuck_frames: number;
  last_summary: string;
  last_suggestion: string;
  last_frame_at: number;
  frame_count: number;
  last_analysis_ms: number;
  alert_level: number;
}

const STATE_META = {
  progress: { icon: CheckCircle2, label: "有进展", cls: "p-status-progress" },
  stuck: { icon: AlertTriangle, label: "卡住", cls: "p-status-stuck" },
  off_track: { icon: XCircle, label: "跑偏", cls: "p-status-off_track" },
  unknown: { icon: Activity, label: "未知", cls: "p-status-unknown" },
  offline: { icon: Activity, label: "离线", cls: "p-status-offline" },
} as const;

const APP_ICON = { codex: TerminalSquare, trae: Braces, hermes: Code2 } as const;

function fmtTime(ts: number) {
  if (!ts) return "--:--:--";
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

interface MonitorPanelProps {
  sessions: SessionData[];
  onClose?: () => void;
}

export function MonitorPanel({ sessions, onClose }: MonitorPanelProps) {
  const rows = useMemo(() => [...sessions].sort((a, b) => a.app_id.localeCompare(b.app_id)), [sessions]);

  return (
    <div className="p-panel p-panel--monitor">
      <div className="p-head">
        <span className="p-title">监控面板</span>
        {onClose && (
          <button type="button" className="p-close" onClick={onClose} aria-label="关闭监控面板">
            <X size={16} strokeWidth={2} aria-hidden="true" />
          </button>
        )}
      </div>

      <div className="p-panel-body">
        <div className="p-card">
        <div className="p-card-title">运行中的应用</div>
        {rows.length === 0 && <div className="p-empty">暂无监控目标，请检查 config/monitors.yaml</div>}
        {rows.map((s) => {
          const meta = STATE_META[s.state] ?? STATE_META.unknown;
          const Icon = APP_ICON[s.app_id as keyof typeof APP_ICON] ?? Code2;
          const StateIcon = meta.icon;
          return (
            <div key={s.app_id} className="p-monitor-row">
              <div className="p-monitor-main">
                <div className="p-monitor-app" title={s.app_name}>
                  <Icon size={16} strokeWidth={1.8} className="p-monitor-app-icon" aria-hidden="true" />
                  <span className="p-monitor-app-name">{s.app_name}</span>
                </div>
                <span className={`p-status ${meta.cls}`} role="status" aria-label={`${s.app_name} 状态：${meta.label}`}>
                  <StateIcon size={12} strokeWidth={2.2} aria-hidden="true" />
                  <span>{meta.label}</span>
                </span>
              </div>
              <div className="p-monitor-summary" title={s.last_summary}>
                {s.last_summary || "—"}
              </div>
              <div className="p-monitor-meta">
                <span>已分析 {s.frame_count} 帧</span>
                <span>·</span>
                <span>最近画面 {fmtTime(s.last_frame_at)}</span>
              </div>
            </div>
          );
        })}
        </div>
      </div>
    </div>
  );
}
