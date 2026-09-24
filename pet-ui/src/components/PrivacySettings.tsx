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

    </div>
  );
}
