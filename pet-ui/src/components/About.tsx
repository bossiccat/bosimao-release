/**
 * 关于页（阶段 C · UI 第二波）
 *
 * - 版本号（对齐 tauri.conf.json 1.0.0）
 * - 隐私说明入口（复用 PrivacyNotice）
 * - 服务进程说明：哪些进程在跑、为什么（本地化/透明，不夸大）
 */
import { useState } from "react";
import {
  Box,
  BrainCircuit,
  ChevronDown,
  ChevronLeft,
  Info,
  Server,
  ShieldCheck,
  X,
} from "lucide-react";
import { PrivacyNotice } from "./PrivacyNotice";

const APP_VERSION = "1.0.0";

const PROCESSES = [
  {
    icon: Box,
    name: "桌宠主进程（Tauri）",
    why: "透明窗口、监控面板与设置界面，是你在桌面看到的入口。",
  },
  {
    icon: Server,
    name: "语音组件 sidecar",
    why: "承载 TRTC 实时语音收发，处理手机端音频的中继与播放订阅。",
  },
  {
    icon: BrainCircuit,
    name: "后端服务（FastAPI）",
    why: "本机控制面与监控判定，协调会话签发、隐私开关与设备管理。",
  },
  {
    icon: Info,
    name: "模型服务（MiniCPM-o）",
    why: "本地语音推理，响应你的语音对话；不依赖第三方云端。",
  },
] as const;

export function About({
  onBack,
  onClose,
}: {
  onBack: () => void;
  onClose: () => void;
}) {
  const [showPrivacy, setShowPrivacy] = useState(false);

  return (
    <div className="about-panel" role="dialog" aria-label="关于">
      <div className="ab-head">
        <button type="button" className="ab-back" onClick={onBack} aria-label="返回设置">
          <ChevronLeft size={16} strokeWidth={2} aria-hidden="true" />
        </button>
        <span className="ab-title">关于</span>
        <button type="button" className="ab-close" onClick={onClose} aria-label="关闭关于">
          <X size={14} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>

      <div className="ab-section">
        <div className="ab-brand">
          <span className="ab-name">贾克斯 · 星核</span>
          <span className="ab-version mono">版本 {APP_VERSION}</span>
        </div>
        <p className="ab-desc">
          波斯猫双工语音助手桌宠，实时监护你的开发进度，并用语音与你对话。
        </p>
      </div>

      <div className="ab-section">
        <div className="ab-section-title">运行中的服务进程</div>
        <ul className="ab-process-list">
          {PROCESSES.map((p) => {
            const Icon = p.icon;
            return (
              <li key={p.name} className="ab-process">
                <span className="ab-process-head">
                  <Icon size={13} strokeWidth={2} aria-hidden="true" />
                  <span>{p.name}</span>
                </span>
                <span className="ab-process-why">{p.why}</span>
              </li>
            );
          })}
        </ul>
      </div>

      <div className="ab-section">
        <button
          type="button"
          className="ab-privacy-toggle"
          aria-expanded={showPrivacy}
          onClick={() => setShowPrivacy((v) => !v)}
        >
          <span className="ab-privacy-label">
            <ShieldCheck size={13} strokeWidth={2} aria-hidden="true" />
            <span>隐私说明</span>
          </span>
          <ChevronDown
            size={13}
            strokeWidth={2}
            className={showPrivacy ? "ab-chevron-open" : ""}
            aria-hidden="true"
          />
        </button>
        {showPrivacy && <PrivacyNotice />}
      </div>

      <style>{`
        .about-panel {
          background: var(--surface);
          border: 1px solid var(--border);
          border-radius: 12px;
          width: 300px;
          font-size: 13px;
          box-shadow: var(--elev-modal);
          overflow: hidden;
        }
        .ab-head {
          display: flex; align-items: center; gap: 8px;
          padding: 10px 14px;
          border-bottom: 1px solid var(--border-soft);
        }
        .ab-title {
          flex: 1;
          font-family: var(--font-display);
          font-weight: var(--weight-announce);
          font-size: 14px;
        }
        .ab-back, .ab-close {
          display: inline-flex; align-items: center; justify-content: center;
          width: var(--target-min); height: var(--target-min);
          border: none; border-radius: 6px;
          background: transparent; color: var(--muted); cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard), color var(--motion-fast) var(--ease-standard);
        }
        .ab-back:hover, .ab-close:hover { background: var(--surface-raised); color: var(--fg); }
        .ab-back:focus-visible, .ab-close:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .ab-section { padding: 10px 14px; border-bottom: 1px solid var(--border-soft); }
        .ab-section:last-child { border-bottom: none; }
        .ab-section-title {
          font-family: var(--font-mono); font-size: 11px;
          color: var(--muted); margin-bottom: 8px;
          letter-spacing: 0.06em; text-transform: uppercase;
        }
        .ab-brand { display: flex; flex-direction: column; gap: 2px; margin-bottom: 6px; }
        .ab-name {
          font-family: var(--font-display);
          font-weight: var(--weight-announce); font-size: 15px;
        }
        .ab-version { font-size: 11px; color: var(--muted); }
        .ab-desc { font-size: 12px; line-height: var(--leading-body); color: var(--fg-2); }
        .ab-process-list { list-style: none; }
        .ab-process {
          display: flex; flex-direction: column; gap: 2px;
          padding: 6px 0;
          border-top: 1px solid var(--border-soft);
        }
        .ab-process:first-of-type { border-top: none; }
        .ab-process-head {
          display: flex; align-items: center; gap: 6px;
          color: var(--fg); font-size: 12px; font-weight: var(--weight-emphasize);
        }
        .ab-process-why { font-size: 12px; line-height: var(--leading-body); color: var(--fg-2); }
        .ab-privacy-toggle {
          display: flex; align-items: center; justify-content: space-between;
          width: 100%;
          padding: 0;
          border: none; background: transparent;
          color: var(--fg-2); cursor: pointer;
          font-size: 12px; font-family: var(--font-body);
          transition: color var(--motion-fast) var(--ease-standard);
        }
        .ab-privacy-toggle:hover { color: var(--fg); }
        .ab-privacy-toggle:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; border-radius: 6px; }
        .ab-privacy-label { display: inline-flex; align-items: center; gap: 6px; }
        .ab-chevron-open { transform: rotate(180deg); }
        @media (prefers-reduced-motion: reduce) {
          .ab-chevron-open { transform: none; }
        }
      `}</style>
    </div>
  );
}
