/**
 * 设置面板（MVP）— 与 docs/SPEC.md §4.2 契约一致
 * - 监控目标开关列表（调 WS control start/stop_monitoring，target=app_id）
 * - 检测阈值只读展示（config/detection.yaml 当前值）
 * - 主题切换（浅色 token 预留，data-theme="light"）
 * - 推送测试：POST /api/v1/control/test-push
 * 第5节规格：白底卡片 + --elev-raised + --space-5。样式见 styles/shell.css。
 */
import { useEffect, useState } from "react";
import {
  Activity,
  Bell,
  CheckCircle2,
  Info,
  Loader2,
  Moon,
  Sun,
  X,
  XCircle,
} from "lucide-react";
import { PrivacySettings } from "./PrivacySettings";
import { DeviceManager } from "./DeviceManager";

export interface MonitorTarget {
  app_id: string;
  app_name: string;
  enabled: boolean;
}

export type SettingsView = "main" | "diagnostics" | "about";

interface SettingsProps {
  targets: MonitorTarget[];
  onToggleTarget: (appId: string, enabled: boolean) => void;
  onClose: () => void;
  onNavigate: (view: Exclude<SettingsView, "main">) => void;
}

/** config/detection.yaml 当前阈值（只读展示；热重载后由后端推送为准） */
const THRESHOLDS = [
  { label: "卡住判定", value: "连续 3 帧不变 · 120s 超时" },
  { label: "跑偏判定", value: "连续 2 帧 off_track" },
  { label: "提醒节流", value: "≥60s 间隔 · ≤30 条/时" },
] as const;

type PushState = "idle" | "sending" | "ok" | "fail";

const PUSH_API = "https://127.0.0.1:8000/api/v1/control/test-push";

export function Settings({ targets, onToggleTarget, onClose, onNavigate }: SettingsProps) {
  const [enabledMap, setEnabledMap] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(targets.map((t) => [t.app_id, t.enabled]))
  );
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    if (typeof localStorage === "undefined") return "dark";
    return localStorage.getItem("pet-theme") === "light" ? "light" : "dark";
  });
  const [push, setPush] = useState<PushState>("idle");
  const [pushMsg, setPushMsg] = useState("");

  // 主题切换：浅色 token 已预留（tokens.css [data-theme="light"]），默认深色
  useEffect(() => {
    if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
    else document.documentElement.removeAttribute("data-theme");
    localStorage.setItem("pet-theme", theme);
  }, [theme]);

  const toggle = (appId: string) => {
    const next = !enabledMap[appId];
    setEnabledMap((m) => ({ ...m, [appId]: next }));
    onToggleTarget(appId, next);
  };

  const testPush = async () => {
    setPush("sending");
    setPushMsg("");
    try {
      const res = await fetch(PUSH_API, { method: "POST" });
      const data = (await res.json().catch(() => null)) as { ok?: boolean; provider?: string; error?: string | null } | null;
      if (res.ok && data?.ok) {
        setPush("ok");
        setPushMsg(data.provider ? `已送达 ${data.provider}` : "已送达");
      } else {
        setPush("fail");
        setPushMsg(data?.error ?? `HTTP ${res.status}`);
      }
    } catch {
      setPush("fail");
      setPushMsg("后端未连接");
    }
  };

  return (
    <div className="settings-panel" role="dialog" aria-label="设置">
      <div className="st-head">
        <span className="st-title">设置</span>
        <button type="button" className="st-close" onClick={onClose} aria-label="关闭设置">
          <X size={14} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>

      <div className="st-section">
        <div className="st-section-title">监控目标</div>
        {targets.length === 0 && <div className="st-empty">暂无监控目标</div>}
        {targets.map((t) => (
          <button
            key={t.app_id}
            type="button"
            className="st-target"
            aria-pressed={enabledMap[t.app_id]}
            onClick={() => toggle(t.app_id)}
          >
            <span className="st-target-name">{t.app_name}</span>
            <span className={`st-switch ${enabledMap[t.app_id] ? "on" : ""}`} aria-hidden="true" />
          </button>
        ))}
      </div>

      <div className="st-section">
        <div className="st-section-title">检测阈值</div>
        {THRESHOLDS.map((row) => (
          <div key={row.label} className="st-threshold">
            <span className="st-th-label">{row.label}</span>
            <span className="st-th-value mono">{row.value}</span>
          </div>
        ))}
      </div>

      <div className="st-section">
        <DeviceManager />
      </div>

      <div className="st-section">
        <PrivacySettings />
      </div>

      <div className="st-section">
        <div className="st-section-title">外观</div>
        <button
          type="button"
          className="st-row-btn"
          onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        >
          {theme === "dark" ? <Sun size={14} strokeWidth={2} aria-hidden="true" /> : <Moon size={14} strokeWidth={2} aria-hidden="true" />}
          <span>{theme === "dark" ? "切换浅色主题" : "切换深色主题"}</span>
        </button>
      </div>

      <div className="st-section">
        <div className="st-section-title">推送</div>
        <button type="button" className="st-row-btn" onClick={testPush} disabled={push === "sending"}>
          {push === "sending" ? (
            <Loader2 size={14} strokeWidth={2} className="st-spin" aria-hidden="true" />
          ) : push === "ok" ? (
            <CheckCircle2 size={14} strokeWidth={2} aria-hidden="true" />
          ) : push === "fail" ? (
            <XCircle size={14} strokeWidth={2} aria-hidden="true" />
          ) : (
            <Bell size={14} strokeWidth={2} aria-hidden="true" />
          )}
          <span>测试推送</span>
          {pushMsg && <span className="st-push-msg mono">{pushMsg}</span>}
        </button>
      </div>

      <div className="st-section">
        <div className="st-nav">
          <button type="button" className="st-nav-btn" onClick={() => onNavigate("diagnostics")}>
            <Activity size={14} strokeWidth={2} aria-hidden="true" />
            <span>运行诊断</span>
          </button>
          <button type="button" className="st-nav-btn" onClick={() => onNavigate("about")}>
            <Info size={14} strokeWidth={2} aria-hidden="true" />
            <span>关于</span>
          </button>
        </div>
      </div>
    </div>
  );
}
