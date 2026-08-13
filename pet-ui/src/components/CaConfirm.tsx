/**
 * CA 安装明示确认弹窗（ADR-020 A2 + 总监红线）
 *
 * 把自签根 CA 装进「当前用户受信根库」属受信面扩张，必须明示用户、可取消、幂等。
 * 用户点「同意并安装」才 invoke install_trusted_ca；「暂不安装」跳过（wss 会连不上，
 * 但用户已被告知，属自主选择，绝不静默降级明文）。
 */
import { useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { ShieldCheck } from "lucide-react";

export function CaConfirm({ onClose }: { onClose: () => void }) {
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
          <ShieldCheck size={20} strokeWidth={1.8} aria-hidden="true" />
          <span id="ca-confirm-title" className="ca-confirm-title">
            安装本地安全证书
          </span>
        </div>

        <p id="ca-confirm-desc" className="ca-confirm-desc">
          为加密本机语音通信，贾克斯·星核需要安装本地安全证书（自签名 CA）。
          该证书仅用于本机回环（127.0.0.1）HTTPS/WSS 加密，不用于远程连接。
        </p>

        {/* 隐私说明入口占位：阶段 B 接「设置 → 隐私」说明页后替换为可点击链接 */}
        <p className="ca-confirm-privacy">可在「设置 → 隐私」查看详细说明。</p>

        {error && (
          <p className="ca-confirm-error" role="alert">
            安装失败，请重试或联系支持。
          </p>
        )}

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

      <style>{`
        .ca-confirm-overlay {
          position: fixed; inset: 0; z-index: 70;
          display: flex; align-items: center; justify-content: center;
          background: var(--overlay);
          padding: var(--space-4);
        }
        .ca-confirm-card {
          background: var(--surface);
          border: 1px solid var(--border);
          border-radius: var(--radius-lg);
          padding: var(--space-5);
          max-width: 340px;
          box-shadow: var(--elev-modal);
          color: var(--fg);
        }
        .ca-confirm-head {
          display: flex; align-items: center; gap: var(--space-2);
          margin-bottom: var(--space-3);
        }
        .ca-confirm-title {
          font-family: var(--font-display);
          font-weight: var(--weight-announce);
          font-size: 15px;
        }
        .ca-confirm-desc {
          font-size: 13px; line-height: var(--leading-body);
          color: var(--fg-2);
          margin-bottom: var(--space-3);
        }
        .ca-confirm-privacy {
          font-size: 12px; color: var(--muted);
          margin-bottom: var(--space-4);
        }
        .ca-confirm-error {
          font-size: 12px; color: var(--danger);
          margin-bottom: var(--space-3);
        }
        .ca-confirm-actions {
          display: flex; gap: var(--space-2);
        }
        .ca-confirm-primary {
          flex: 1; min-height: 40px;
          display: inline-flex; align-items: center; justify-content: center;
          background: var(--button-primary-bg); color: var(--button-primary-fg);
          border: none; border-radius: var(--radius-md);
          font-size: 13px; font-weight: var(--weight-emphasize); cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .ca-confirm-primary:hover:not(:disabled) { background: var(--button-primary-hover); }
        .ca-confirm-primary:disabled { opacity: 0.6; cursor: default; }
        .ca-confirm-secondary {
          flex: 1; min-height: 40px;
          display: inline-flex; align-items: center; justify-content: center;
          background: var(--button-secondary-bg); color: var(--button-secondary-fg);
          border: 1px solid var(--border); border-radius: var(--radius-md);
          font-size: 13px; font-weight: var(--weight-emphasize); cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .ca-confirm-secondary:hover:not(:disabled) { background: var(--surface-raised); }
        .ca-confirm-secondary:disabled { opacity: 0.6; cursor: default; }
        .ca-confirm-primary:focus-visible,
        .ca-confirm-secondary:focus-visible {
          outline: 2px solid var(--focus); outline-offset: 2px;
          box-shadow: var(--focus-ring);
        }
      `}</style>
    </div>
  );
}
