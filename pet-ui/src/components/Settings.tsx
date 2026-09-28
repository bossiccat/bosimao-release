/**
 * 设置面板（§5.3 暖瓷）— 紧凑标题行 + 分组 + 行式开关。
 * 颜色全部来自 design-tokens.css；样式见 styles/shell.css .settings-panel/.st-*。
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
    <div className="settings-panel" role="dialog" aria-label="设置">
      <div className="st-head">
        <span className="st-title">设置</span>
        <button type="button" className="st-close" onClick={onClose} aria-label="关闭设置">
          <X size={16} strokeWidth={1.75} aria-hidden="true" />
        </button>
      </div>

      <div className="st-body">
        <div className="st-group">
          <div className="st-group-title">监控目标</div>
          {targets.length === 0 && <div className="st-empty">还没有可监控的目标</div>}
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

        <div className="st-group">
          <div className="st-group-title">外观</div>
          <button
            type="button"
            className="st-row-btn"
            onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
          >
            {theme === "dark" ? (
              <Sun size={16} strokeWidth={1.75} aria-hidden="true" />
            ) : (
              <Moon size={16} strokeWidth={1.75} aria-hidden="true" />
            )}
            <span>{theme === "dark" ? "浅色外观" : "深色外观"}</span>
          </button>
        </div>

        <div className="st-group">
          <div className="st-group-title">推送</div>
          <button type="button" className="st-row-btn" onClick={testPush} disabled={push === "sending"}>
            {push === "sending" ? (
              <Loader2 size={16} strokeWidth={1.75} className="st-spin" aria-hidden="true" />
            ) : push === "ok" ? (
              <CheckCircle2 size={16} strokeWidth={1.75} aria-hidden="true" />
            ) : push === "fail" ? (
              <XCircle size={16} strokeWidth={1.75} aria-hidden="true" />
            ) : (
              <Bell size={16} strokeWidth={1.75} aria-hidden="true" />
            )}
            <span>测试推送</span>
            {pushMsg && <span className="st-row-btn-msg">{pushMsg}</span>}
          </button>
        </div>

        <div className="st-group">
          <button
            type="button"
            className="st-advanced-toggle"
            aria-expanded={advancedOpen}
            onClick={() => setAdvancedOpen((v) => !v)}
          >
            <span>高级</span>
            <ChevronDown
              size={16}
              strokeWidth={1.75}
              className={`st-advanced-chevron ${advancedOpen ? "open" : ""}`}
              aria-hidden="true"
            />
          </button>
          {advancedOpen && (
            <div className="st-advanced-body">
              {ADVANCED_ROWS.map((row) => (
                <div key={row.label} className="st-advanced-row">
                  <span className="st-advanced-label">{row.label}</span>
                  <span className="st-advanced-value">{row.value}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="st-group">
          <DeviceManager />
        </div>

        <div className="st-group">
          <PrivacySettings />
        </div>

        <div className="st-group">
          <div className="st-nav">
            <button type="button" className="st-nav-btn" onClick={() => onNavigate("diagnostics")}>
              <Activity size={16} strokeWidth={1.75} aria-hidden="true" />
              <span>运行诊断</span>
            </button>
            <button type="button" className="st-nav-btn" onClick={() => onNavigate("about")}>
              <Info size={16} strokeWidth={1.75} aria-hidden="true" />
              <span>关于</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
