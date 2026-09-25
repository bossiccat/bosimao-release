/**
 * 设置面板（商业化第三版）
 * - 紧凑标题行 + 分组卡片 + 行式布局
 * - 运维阈值收进「高级」折叠区，主界面只暴露用户语言
 * - 颜色全部来自 design-tokens.css
 */
import { useEffect, useState } from "react";
import {
  Activity,
  Bell,
  CheckCircle2,
  ChevronDown,
  Info,
  Loader2,
  Moon,
  Sun,
  X,
  XCircle,
} from "lucide-react";
import { PrivacySettings } from "./PrivacySettings";
import { DeviceManager } from "./DeviceManager";
import "../styles/panels.css";

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

type PushState = "idle" | "sending" | "ok" | "fail";

const PUSH_API = "https://127.0.0.1:8000/api/v1/control/test-push";

/** 运维阈值：仅出现在「高级」折叠区，使用用户可读文案 */
const ADVANCED_ROWS = [
  { label: "检测灵敏度", value: "标准（连续 3 帧无变化，2 分钟超时）" },
  { label: "偏离判定", value: "标准（连续 2 次偏离主线）" },
  { label: "提醒频率", value: "标准（至少间隔 1 分钟，每小时最多 30 条）" },
] as const;

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
  const [advancedOpen, setAdvancedOpen] = useState(false);

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
    <div className="p-panel p-panel--settings" role="dialog" aria-label="设置">
      <div className="p-head">
        <span className="p-title">设置</span>
        <button type="button" className="p-close" onClick={onClose} aria-label="关闭设置">
          <X size={16} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>

      <div className="p-panel-body">
        <div className="p-card">
        <div className="p-card-title">监控目标</div>
        {targets.length === 0 && <div className="p-empty">暂无监控目标</div>}
        {targets.map((t) => (
          <button
            key={t.app_id}
            type="button"
            className="p-row"
            aria-pressed={enabledMap[t.app_id]}
            onClick={() => toggle(t.app_id)}
          >
            <span className="p-row-label">{t.app_name}</span>
            <span className="p-row-control">
              <span className={`p-switch ${enabledMap[t.app_id] ? "on" : ""}`} aria-hidden="true" />
            </span>
          </button>
        ))}
      </div>

      <div className="p-card">
        <div className="p-card-title">外观</div>
        <button
          type="button"
          className="p-row-btn"
          onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        >
          {theme === "dark" ? (
            <Sun size={16} strokeWidth={2} aria-hidden="true" />
          ) : (
            <Moon size={16} strokeWidth={2} aria-hidden="true" />
          )}
          <span className="p-row-label">{theme === "dark" ? "浅色外观" : "深色外观"}</span>
        </button>
      </div>

      <div className="p-card">
        <div className="p-card-title">推送</div>
        <button type="button" className="p-row-btn" onClick={testPush} disabled={push === "sending"}>
          {push === "sending" ? (
            <Loader2 size={16} strokeWidth={2} className="p-spin" aria-hidden="true" />
          ) : push === "ok" ? (
            <CheckCircle2 size={16} strokeWidth={2} aria-hidden="true" />
          ) : push === "fail" ? (
            <XCircle size={16} strokeWidth={2} aria-hidden="true" />
          ) : (
            <Bell size={16} strokeWidth={2} aria-hidden="true" />
          )}
          <span className="p-row-label">测试推送</span>
          {pushMsg && <span className="p-row-btn-msg">{pushMsg}</span>}
        </button>
      </div>

      <div className="p-card">
        <button
          type="button"
          className="p-advanced-toggle"
          aria-expanded={advancedOpen}
          onClick={() => setAdvancedOpen((v) => !v)}
        >
          <span>高级</span>
          <ChevronDown
            size={16}
            strokeWidth={2}
            className={`p-advanced-chevron ${advancedOpen ? "open" : ""}`}
            aria-hidden="true"
          />
        </button>
        {advancedOpen && (
          <div className="p-advanced-body">
            {ADVANCED_ROWS.map((row) => (
              <div key={row.label} className="p-advanced-row">
                <span className="p-advanced-label">{row.label}</span>
                <span className="p-advanced-value">{row.value}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="p-card">
        <DeviceManager />
      </div>

      <div className="p-card">
        <PrivacySettings />
      </div>

      <div className="p-nav">
        <button type="button" className="p-nav-btn" onClick={() => onNavigate("diagnostics")}>
          <Activity size={16} strokeWidth={2} aria-hidden="true" />
          <span>运行诊断</span>
        </button>
        <button type="button" className="p-nav-btn" onClick={() => onNavigate("about")}>
          <Info size={16} strokeWidth={2} aria-hidden="true" />
          <span>关于</span>
        </button>
      </div>
      </div>
    </div>
  );
}
