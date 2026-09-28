/**
 * 监控面板（§5.4 暖瓷）— 头部 + 汇总条 + 目标行（grid 104/72/1fr）+ 空状态（爪印）。
 * 颜色全部来自 design-tokens.css；样式见 styles/shell.css .monitor-panel/.mp-*。
 */
import { useMemo } from "react";
import { Activity, AlertTriangle, CheckCircle2, Code2, TerminalSquare, Braces, X, XCircle } from "lucide-react";
import { JaxPaw } from "./icons";

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
  progress: { icon: CheckCircle2, label: "有进展", state: "progress" as const },
  stuck: { icon: AlertTriangle, label: "卡住", state: "stuck" as const },
  off_track: { icon: XCircle, label: "跑偏", state: "off_track" as const },
  unknown: { icon: Activity, label: "未知", state: "unknown" as const },
  offline: { icon: Activity, label: "离线", state: "offline" as const },
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
  onOpenSettings?: () => void;
}

export function MonitorPanel({ sessions, onClose, onOpenSettings }: MonitorPanelProps) {
  const rows = useMemo(() => [...sessions].sort((a, b) => a.app_id.localeCompare(b.app_id)), [sessions]);
  const inProgress = rows.filter((s) => s.state === "progress").length;

  return (
    <div className="monitor-panel" role="dialog" aria-label="监控面板">
      <div className="mp-head" data-tauri-drag-region>
        <span className="mp-title">监控面板</span>
        {onClose && (
          <button type="button" className="mp-close" onClick={onClose} aria-label="关闭监控面板">
            <X size={16} strokeWidth={1.75} aria-hidden="true" />
          </button>
        )}
      </div>

      <div className="mp-body">
        <div className="mp-summary-bar">
          <span>{rows.length} 个目标</span>
          <span>·</span>
          <span><b>{inProgress}</b> 项进行中</span>
        </div>

        {rows.length === 0 ? (
          <div className="mp-empty">
            <span className="mp-empty-icon">
              <JaxPaw size={32} strokeWidth={1.75} />
            </span>
            <span className="mp-empty-text">还没有可监控的目标</span>
            {onOpenSettings && (
              <button type="button" className="mp-empty-btn" onClick={onOpenSettings}>
                去添加监控目标
              </button>
            )}
          </div>
        ) : (
          rows.map((s) => {
            const meta = STATE_META[s.state] ?? STATE_META.unknown;
            const Icon = APP_ICON[s.app_id as keyof typeof APP_ICON] ?? Code2;
            const StateIcon = meta.icon;
            return (
              <div key={s.app_id} className="mp-row">
                <div className="mp-app" title={s.app_name}>
                  <Icon size={16} strokeWidth={1.75} className="mp-app-icon" aria-hidden="true" />
                  <span className="mp-app-name">{s.app_name}</span>
                </div>
                <span className="mp-status" data-state={meta.state} role="status" aria-label={`${s.app_name} 状态：${meta.label}`}>
                  <StateIcon size={12} strokeWidth={2.2} aria-hidden="true" />
                  <span>{meta.label}</span>
                </span>
                <span className="mp-summary" title={s.last_summary}>
                  {s.last_summary || "—"}
                </span>
                <div className="mp-meta">
                  <span>已分析 {s.frame_count} 帧</span>
                  <span>·</span>
                  <span>最近画面 {fmtTime(s.last_frame_at)}</span>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
