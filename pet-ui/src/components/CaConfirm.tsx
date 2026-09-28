/**
 * CA 安装明示确认弹窗（§5.5 暖瓷）— 头区绿雾渐变 + 盾徽；卡片化信息区、隐私说明、操作按钮。
 * 颜色全部来自 design-tokens.css；样式见 styles/shell.css .ca-confirm-*。
 */
import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { ChevronDown, ShieldCheck, X } from "lucide-react";
import { PrivacyNotice } from "./PrivacyNotice";

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
      className="ca-confirm-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="ca-confirm-title"
      aria-describedby="ca-confirm-desc"
    >
      <div className="ca-confirm-card">
        <div className="ca-confirm-head">
          <button type="button" className="ca-confirm-close" onClick={onClose} aria-label="关闭">
            <X size={16} strokeWidth={1.75} aria-hidden="true" />
          </button>
          <span className="ca-confirm-shield" aria-hidden="true">
            <ShieldCheck size={24} strokeWidth={1.75} />
          </span>
          <span id="ca-confirm-title" className="ca-confirm-title">
            安装本地安全证书
          </span>
        </div>

        <div className="ca-confirm-body">
          <p id="ca-confirm-desc" className="ca-confirm-desc">
            为加密本机语音通信，贾克斯·星核需要安装本地安全证书（自签名 CA）。
            该证书仅用于本机回环（<span className="ca-confirm-code">127.0.0.1</span>）HTTPS/WSS 加密，不用于远程连接。
          </p>

          <div className="ca-confirm-privacy">
            <button
              type="button"
              className="ca-confirm-privacy-toggle"
              aria-expanded={showPrivacy}
              onClick={() => setShowPrivacy((v) => !v)}
            >
              <span>查看隐私说明</span>
              <ChevronDown
                size={13}
                strokeWidth={1.75}
                className={`ca-confirm-privacy-chevron ${showPrivacy ? "open" : ""}`}
                aria-hidden="true"
              />
            </button>
            {showPrivacy && <PrivacyNotice />}
          </div>

          {error && (
            <p className="ca-confirm-error" role="alert">
              安装失败，请重试或联系支持。
            </p>
          )}
        </div>

        <div className="ca-confirm-actions">
          <button
            type="button"
            className="ca-confirm-primary"
            onClick={install}
            disabled={installing}
          >
            {installing ? "安装中…" : "同意并安装"}
          </button>
          <button
            type="button"
            className="ca-confirm-secondary"
            onClick={onClose}
            disabled={installing}
          >
            暂不安装
          </button>
        </div>
      </div>
    </div>
  );
}
