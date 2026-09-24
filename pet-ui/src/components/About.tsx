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

    </div>
  );
}
