/**
 * 监控面板 — 三个被监控 App 的状态点 + 时间线 + mono 数据列
 * 与 openapi.yaml AgentSession schema 对应。
 * 第5节规格：白底卡片 + --elev-raised + --space-5；标题 15px/600、正文 13px/400、说明 12px --muted。
 * 样式见 styles/shell.css。
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
  progress: { icon: CheckCircle2, label: "有进展" },
  stuck: { icon: AlertTriangle, label: "卡住" },
  off_track: { icon: XCircle, label: "跑偏" },
  unknown: { icon: Activity, label: "未知" },
  offline: { icon: Activity, label: "离线" },
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
              <div
                className="mp-status"
                data-state={s.state}
                role="status"
                aria-label={`${s.app_name} 状态：${meta.label}`}
              >
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
    </div>
  );
}
