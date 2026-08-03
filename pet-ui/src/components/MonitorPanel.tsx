/**
 * 监控面板 — 三个被监控 App 的状态点 + 时间线 + mono 数据列
 * 与 openapi.yaml AgentSession schema 对应
 */
import { useMemo } from "react";
import { Activity, AlertTriangle, CheckCircle2, Code2, TerminalSquare, Braces, XCircle } from "lucide-react";

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
  progress: { icon: CheckCircle2, color: "var(--success)", label: "有进展" },
  stuck: { icon: AlertTriangle, color: "var(--warn)", label: "卡住" },
  off_track: { icon: XCircle, color: "var(--danger)", label: "跑偏" },
  unknown: { icon: Activity, color: "var(--muted)", label: "未知" },
  offline: { icon: Activity, color: "var(--muted)", label: "离线" },
} as const;

const APP_ICON = { codex: TerminalSquare, trae: Braces, hermes: Code2 } as const;

function fmtTime(ts: number) {
  if (!ts) return "--:--:--";
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

export function MonitorPanel({ sessions }: { sessions: SessionData[] }) {
  const rows = useMemo(() => [...sessions].sort((a, b) => a.app_id.localeCompare(b.app_id)), [sessions]);

  return (
    <div className="monitor-panel">
      <div className="mp-head">
        <span className="mp-title">监控面板</span>
        <span className="mp-count">{rows.length} agents</span>
      </div>
      <div className="mp-body">
        {rows.length === 0 && <div className="mp-empty">暂无监控目标，请检查 config/monitors.yaml</div>}
        {rows.map((s) => {
          const meta = STATE_META[s.state] ?? STATE_META.unknown;
          const Icon = APP_ICON[s.app_id as keyof typeof APP_ICON] ?? Code2;
          const StateIcon = meta.icon;
          return (
            <div key={s.app_id} className="mp-row">
              <div className="mp-app">
                <Icon size={16} strokeWidth={1.8} aria-hidden="true" />
                <span className="mp-app-name">{s.app_name}</span>
              </div>
              <div className="mp-status" style={{ color: meta.color }} role="status" aria-label={`${s.app_name} 状态：${meta.label}`}>
                <StateIcon size={14} strokeWidth={2.2} aria-hidden="true" />
                <span>{meta.label}</span>
              </div>
              <div className="mp-summary" title={s.last_summary}>{s.last_summary || "—"}</div>
              <div className="mp-meta mono">
                <span>{s.frame_count}帧</span>
                <span>{s.last_analysis_ms}ms</span>
                <span>{fmtTime(s.last_frame_at)}</span>
              </div>
            </div>
          );
        })}
      </div>
      <style>{`
        .monitor-panel {
          background: var(--surface);
          border: 1px solid var(--border);
          border-radius: 12px;
          width: 340px;
          font-size: 13px;
          overflow: hidden;
        }
        .mp-head {
          display: flex; justify-content: space-between; align-items: center;
          padding: 10px 14px;
          border-bottom: 1px solid var(--border-soft);
          font-family: var(--font-display);
          font-weight: 590;
          font-size: 14px;
        }
        .mp-count { color: var(--fg-2); font-family: var(--font-mono); font-size: 12px; font-weight: 400; }
        .mp-body { padding: 6px 0; max-height: 260px; overflow-y: auto; }
        .mp-row {
          display: grid;
          grid-template-columns: 96px 64px 1fr;
          gap: 8px; align-items: center;
          padding: 8px 14px;
          border-bottom: 1px solid var(--border-soft);
        }
        .mp-row:last-child { border-bottom: none; }
        .mp-app { display: flex; gap: 6px; align-items: center; color: var(--fg-2); }
        .mp-app-name { white-space: nowrap; }
        .mp-status { display: flex; gap: 4px; align-items: center; font-size: 12px; font-weight: 510; }
        .mp-summary { color: var(--fg-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .mp-meta {
          grid-column: 1 / -1;
          display: flex; gap: 12px;
          color: var(--fg-2); font-size: 12px; /* 对比度 ≥4.5:1（原 --muted 11px 不达标） */
        }
        .mp-empty { padding: 20px 14px; color: var(--muted); text-align: center; }
        .mono { font-family: var(--font-mono); }
      `}</style>
    </div>
  );
}
