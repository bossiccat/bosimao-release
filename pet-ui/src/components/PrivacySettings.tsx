/**
 * 隐私开关区（阶段 B-2）— ADR-021 D2 契约
 *
 * - 挂载时 GET /api/v1/privacy 读当前值；读失败 fail-closed：不展示默认「开」的假开关。
 * - 切换时 PATCH /api/v1/privacy/{setting}：成功保持新值；失败回滚到旧值并提示。
 * - 信任红线：microphone / background 旁明示「该开关需手机端配合生效」（后端只存状态，
 *   Android 实时停采集留后续迭代），绝不做成「看起来关了其实没生效」的假开关。
 */
import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ChevronDown,
  Cloud,
  FileText,
  Loader2,
  MessageSquare,
  Mic,
  Monitor,
  ShieldCheck,
  Smartphone,
} from "lucide-react";
import {
  fetchPrivacySettings,
  setPrivacySetting,
  type PrivacySettingKey,
  type PrivacySettingPath,
  type PrivacySettings,
} from "../lib/privacy";
import { PrivacyNotice } from "./PrivacyNotice";

interface PrivacyItem {
  key: PrivacySettingKey;
  path: PrivacySettingPath;
  icon: typeof Cloud;
  label: string;
  phoneHint?: boolean;
}

const PRIVACY_ITEMS: PrivacyItem[] = [
  { key: "cloud_processing_enabled", path: "cloud_processing", icon: Cloud, label: "云端处理" },
  { key: "microphone_enabled", path: "microphone", icon: Mic, label: "麦克风", phoneHint: true },
  {
    key: "background_conversation_enabled",
    path: "background_conversation",
    icon: MessageSquare,
    label: "后台对话",
    phoneHint: true,
  },
  { key: "desktop_capture_enabled", path: "desktop_capture", icon: Monitor, label: "桌面捕获" },
  {
    key: "transcript_persistence_enabled",
    path: "transcript_persistence",
    icon: FileText,
    label: "转写持久化",
  },
];

type LoadState = "loading" | "ok" | "error";

export function PrivacySettings() {
  const [settings, setSettings] = useState<PrivacySettings | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [pending, setPending] = useState<Set<PrivacySettingKey>>(new Set());
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [showNotice, setShowNotice] = useState(false);

  const load = async () => {
    setLoadState("loading");
    setErrorMsg(null);
    try {
      const next = await fetchPrivacySettings();
      setSettings(next);
      setLoadState("ok");
    } catch {
      setSettings(null);
      setLoadState("error");
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const toggle = async (item: PrivacyItem) => {
    if (loadState !== "ok" || !settings) return;
    const prev = settings[item.key];
    const next = !prev;

    // 乐观更新（先更新 UI，失败回滚）
    setSettings((s) => (s ? { ...s, [item.key]: next } : s));
    setPending((p) => new Set(p).add(item.key));
    setErrorMsg(null);

    try {
      await setPrivacySetting(item.path, next);
      // 成功：保持新值
    } catch {
      // 失败：回滚 UI 到旧值并提示（AC-17「失败必须回滚 UI 设置」）
      setSettings((s) => (s ? { ...s, [item.key]: prev } : s));
      setErrorMsg(`「${item.label}」切换失败，已恢复原状态`);
    } finally {
      setPending((p) => {
        const n = new Set(p);
        n.delete(item.key);
        return n;
      });
    }
  };

  return (
    <div className="privacy-settings" role="group" aria-label="隐私设置">
      <div className="ps-head">
        <ShieldCheck size={14} strokeWidth={2} aria-hidden="true" />
        <span className="ps-head-label">隐私</span>
      </div>

      {loadState === "loading" && (
        <div className="ps-state">
          <Loader2 size={13} strokeWidth={2} className="ps-spin" aria-hidden="true" />
          <span>正在读取隐私设置…</span>
        </div>
      )}

      {loadState === "error" && (
        <div className="ps-state ps-state-error" role="alert">
          <AlertTriangle size={13} strokeWidth={2} aria-hidden="true" />
          <span>无法读取隐私设置，已停用开关避免误显状态。</span>
          <button type="button" className="ps-retry" onClick={() => void load()}>
            重试
          </button>
        </div>
      )}

      {loadState === "ok" &&
        settings &&
        PRIVACY_ITEMS.map((item) => {
          const Icon = item.icon;
          const on = settings[item.key];
          const isPending = pending.has(item.key);
          return (
            <div key={item.key} className="ps-row">
              <button
                type="button"
                className="ps-switch-row"
                aria-pressed={on}
                disabled={isPending}
                onClick={() => void toggle(item)}
              >
                <Icon size={14} strokeWidth={2} aria-hidden="true" />
                <span className="ps-row-label">{item.label}</span>
                {isPending ? (
                  <Loader2 size={12} strokeWidth={2} className="ps-spin ps-pending" aria-hidden="true" />
                ) : (
                  <span className={`ps-switch ${on ? "on" : ""}`} aria-hidden="true" />
                )}
              </button>
              {item.phoneHint && (
                <span className="ps-phone-hint">
                  <Smartphone size={12} strokeWidth={2} aria-hidden="true" />
                  <span>该开关需手机端配合生效</span>
                </span>
              )}
            </div>
          );
        })}

      {errorMsg && (
        <p className="ps-error" role="alert">
          <AlertTriangle size={12} strokeWidth={2} aria-hidden="true" />
          <span>{errorMsg}</span>
        </p>
      )}

      <button
        type="button"
        className="ps-notice-toggle"
        aria-expanded={showNotice}
        onClick={() => setShowNotice((v) => !v)}
      >
        <span>隐私说明</span>
        <ChevronDown
          size={13}
          strokeWidth={2}
          className={showNotice ? "ps-chevron-open" : ""}
          aria-hidden="true"
        />
      </button>

      {showNotice && <PrivacyNotice />}

      <style>{`
        .privacy-settings {
          display: flex; flex-direction: column; gap: 2px;
        }
        .ps-head {
          display: flex; align-items: center; gap: 6px;
          color: var(--fg-2);
          margin-bottom: 6px;
        }
        .ps-head-label {
          font-family: var(--font-mono);
          font-size: 11px;
          letter-spacing: 0.06em;
          text-transform: uppercase;
          color: var(--muted);
        }
        .ps-state {
          display: flex; align-items: center; gap: 6px;
          font-size: 12px; color: var(--fg-2);
          padding: 6px 0;
        }
        .ps-state-error { color: var(--danger); flex-wrap: wrap; }
        .ps-retry {
          border: 1px solid var(--border);
          border-radius: var(--radius-sm);
          background: transparent;
          color: var(--fg);
          font-size: 12px;
          padding: 2px 8px;
          cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .ps-retry:hover { background: var(--surface-raised); }
        .ps-retry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .ps-row { display: flex; flex-direction: column; }
        .ps-switch-row {
          display: flex; align-items: center; gap: 8px;
          width: 100%;
          padding: 7px 0;
          border: none; background: transparent;
          color: var(--fg); cursor: pointer;
          font-size: 13px; font-family: var(--font-body);
        }
        .ps-switch-row:hover .ps-row-label { color: var(--accent); }
        .ps-switch-row:disabled { cursor: default; }
        .ps-switch-row:disabled .ps-row-label { color: var(--fg-2); }
        .ps-switch-row:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: 6px; }
        .ps-row-label { flex: 1; text-align: left; transition: color var(--motion-fast) var(--ease-standard); }
        .ps-switch {
          width: 30px; height: 16px;
          border-radius: 999px;
          background: var(--surface-raised);
          border: 1px solid var(--border);
          position: relative; flex: none;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .ps-switch::after {
          content: "";
          position: absolute; top: 2px; left: 2px;
          width: 10px; height: 10px;
          border-radius: 50%;
          background: var(--muted);
          transition: transform var(--motion-fast) var(--ease-standard), background-color var(--motion-fast) var(--ease-standard);
        }
        .ps-switch.on { background: var(--accent); border-color: var(--accent); }
        .ps-switch.on::after { transform: translateX(14px); background: #fff; }
        .ps-phone-hint {
          display: flex; align-items: center; gap: 4px;
          padding: 0 0 6px 22px;
          font-size: 11px; color: var(--warn);
        }
        .ps-pending { color: var(--muted); }
        .ps-error {
          display: flex; align-items: center; gap: 6px;
          font-size: 12px; color: var(--danger);
          padding: 4px 0;
        }
        .ps-notice-toggle {
          display: flex; align-items: center; justify-content: space-between;
          width: 100%;
          padding: 8px 0;
          border: none; border-top: 1px solid var(--border-soft);
          background: transparent;
          color: var(--fg-2); cursor: pointer;
          font-size: 12px; font-family: var(--font-body);
          margin-top: 4px;
          transition: color var(--motion-fast) var(--ease-standard);
        }
        .ps-notice-toggle:hover { color: var(--fg); }
        .ps-notice-toggle:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; border-radius: 6px; }
        .ps-chevron-open { transform: rotate(180deg); }
        .ps-spin { animation: ps-spin 0.8s linear infinite; }
        @keyframes ps-spin { to { transform: rotate(360deg); } }
        @media (prefers-reduced-motion: reduce) {
          .ps-spin { animation: none; }
          .ps-chevron-open { transform: none; }
        }
      `}</style>
    </div>
  );
}
