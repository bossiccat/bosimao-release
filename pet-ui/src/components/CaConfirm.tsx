/**
 * CA 安装明示确认弹窗（商业化第三版）
 * - 紧凑标题行 + 关闭按钮
 * - 卡片化信息区、隐私说明、操作按钮
 * - 颜色全部来自 design-tokens.css
 */
import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { ChevronDown, ShieldCheck, X } from "lucide-react";
import { PrivacyNotice } from "./PrivacyNotice";
import "../styles/panels.css";

export function CaConfirm({ onClose }: { onClose: () => void }) {
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showPrivacy, setShowPrivacy] = useState(false);

  const install = async () => {
    setInstalling(true);
    setError(null);
    try {
      await invoke<string>("install_trusted_ca");
      onClose();
    } catch (e) {
      setError(typeof e === "string" ? e : String(e));
      setInstalling(false);
    }
  };

  return (
    <div
      className="p-ca-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="ca-confirm-title"
      aria-describedby="ca-confirm-desc"
    >
      <div className="p-ca-card">
        <div className="p-ca-head">
          <div className="p-ca-title-wrap">
            <ShieldCheck size={20} strokeWidth={1.8} aria-hidden="true" />
            <span id="ca-confirm-title" className="p-ca-title">
              安装本地安全证书
            </span>
          </div>
          <button type="button" className="p-close" onClick={onClose} aria-label="关闭">
            <X size={16} strokeWidth={2} aria-hidden="true" />
          </button>
        </div>

        <div className="p-ca-body">
          <p id="ca-confirm-desc" className="p-ca-desc">
            为加密本机语音通信，贾克斯·星核需要安装本地安全证书（自签名 CA）。
            该证书仅用于本机回环（127.0.0.1）HTTPS/WSS 加密，不用于远程连接。
          </p>

          <div className="p-ca-privacy">
            <button
              type="button"
              className="p-ca-privacy-toggle"
              aria-expanded={showPrivacy}
              onClick={() => setShowPrivacy((v) => !v)}
            >
              <span>查看隐私说明</span>
              <ChevronDown
                size={13}
                strokeWidth={2}
                className={`p-ca-privacy-chevron ${showPrivacy ? "open" : ""}`}
                aria-hidden="true"
              />
            </button>
            {showPrivacy && <PrivacyNotice />}
          </div>

          {error && (
            <p className="p-ca-error" role="alert">
              安装失败，请重试或联系支持。
            </p>
          )}

          <div className="p-ca-actions">
            <button
              type="button"
              className="p-ca-primary"
              onClick={install}
              disabled={installing}
            >
              {installing ? "安装中…" : "同意并安装"}
            </button>
            <button
              type="button"
              className="p-ca-secondary"
              onClick={onClose}
              disabled={installing}
            >
              暂不安装
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
