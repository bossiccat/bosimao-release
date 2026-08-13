/**
 * 运行诊断页（阶段 C · UI 第二波）— DESIGN §5.4
 *
 * 六项：SDK / 模型 / 网络 / 麦克风 / 播放 / sidecar。
 * 数据源：GET /api/v1/status + GET /health + Tauri `get_sidecar_status`。
 * 每行：状态图标（CheckCircle2/CircleAlert/XCircle）+ 项目名 + 分类结果 + 耗时。
 * 状态呈现不依赖颜色：图标 + 文字（可访问性契约 §7）。
 *
 * 诚实边界：麦克风/播放的能力在手机端，桌面后端无实时探测信号，故标记「未检测」
 * 并说明原因，绝不做假「正常」（信任红线）。
 */
import { useEffect, useState } from "react";
import {
  Activity,
  Box,
  BrainCircuit,
  CheckCircle2,
  ChevronLeft,
  CircleAlert,
  Loader2,
  Mic,
  RefreshCw,
  Volume2,
  Wifi,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { invoke, isTauri } from "@tauri-apps/api/core";

type CheckStatus = "ok" | "error" | "unknown";

interface DiagItem {
  id: string;
  label: string;
  icon: LucideIcon;
  status: CheckStatus;
  result: string;
  detail: string;
  elapsedMs: number | null;
}

const HEALTH_API = "https://127.0.0.1:8000/health";
const STATUS_API = "https://127.0.0.1:8000/api/v1/status";

interface HealthBody {
  status?: string;
  model_server?: string;
}

interface StatusBody {
  engine?: { model_loaded?: boolean; vram_mb?: number; inference_busy?: boolean };
  pet_state?: string;
}

function statusMeta(status: CheckStatus): { icon: LucideIcon; result: string; tone: string } {
  switch (status) {
    case "ok":
      return { icon: CheckCircle2, result: "正常", tone: "ok" };
    case "error":
      return { icon: XCircle, result: "异常", tone: "error" };
    default:
      return { icon: CircleAlert, result: "未检测", tone: "unknown" };
  }
}

export function Diagnostics({
  onBack,
  onClose,
}: {
  onBack: () => void;
  onClose: () => void;
}) {
  const [items, setItems] = useState<DiagItem[]>([]);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ current: string; done: number } | null>(null);
  const [lastRunAt, setLastRunAt] = useState<number | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  async function probe<T>(url: string): Promise<{ ok: boolean; data: T | null; elapsedMs: number }> {
    const started = performance.now();
    try {
      const res = await fetch(url);
      const data = (await res.json().catch(() => null)) as T | null;
      return { ok: res.ok, data, elapsedMs: Math.round(performance.now() - started) };
    } catch {
      return { ok: false, data: null, elapsedMs: Math.round(performance.now() - started) };
    }
  }

  const run = async () => {
    setRunning(true);
    setExpandedId(null);

    const step = (current: string, done: number) => setProgress({ current, done });

    // 1. 网络（控制面可达性）
    step("网络", 0);
    const health = await probe<HealthBody>(HEALTH_API);

    // 2. 模型 / SDK（引擎状态）
    step("模型与引擎", 1);
    const status = health.ok ? await probe<StatusBody>(STATUS_API) : { ok: false, data: null, elapsedMs: 0 };

    // 3. sidecar（Tauri 命令）
    step("语音组件", 2);
    let sidecar: "running" | "stopped" | null = null;
    let sidecarElapsed = 0;
    if (isTauri()) {
      const s = performance.now();
      try {
        const v = await invoke<string>("get_sidecar_status");
        sidecar = v === "running" ? "running" : v === "stopped" ? "stopped" : null;
      } catch {
        sidecar = null;
      }
      sidecarElapsed = Math.round(performance.now() - s);
    }

    const engine = status.data?.engine;
    const modelServer = health.data?.model_server;
    const networkOk = health.ok;

    const next: DiagItem[] = [
      {
        id: "sdk",
        label: "SDK 状态",
        icon: BrainCircuit,
        status: engine?.model_loaded === true ? "ok" : networkOk ? "error" : "unknown",
        result: engine?.model_loaded === true ? "正常" : networkOk ? "异常" : "未检测",
        detail:
          engine?.model_loaded === true
            ? "本地推理引擎（MiniCPM-o）已加载，可承接语音会话。"
            : networkOk
              ? "本地推理引擎未加载，语音链路不可用。"
              : "后端不可达，无法读取引擎状态。",
        elapsedMs: status.ok ? status.elapsedMs : null,
      },
      {
        id: "model",
        label: "模型服务",
        icon: Activity,
        status: modelServer === "up" ? "ok" : networkOk ? "error" : "unknown",
        result: modelServer === "up" ? "正常" : networkOk ? "异常" : "未检测",
        detail:
          modelServer === "up"
            ? "模型服务进程在线。"
            : networkOk
              ? "模型服务未就绪，请稍后重试。"
              : "后端不可达，无法读取模型服务状态。",
        elapsedMs: health.elapsedMs,
      },
      {
        id: "network",
        label: "网络",
        icon: Wifi,
        status: networkOk ? "ok" : "error",
        result: networkOk ? "正常" : "异常",
        detail: networkOk
          ? "本机回环后端（127.0.0.1:8000）可达。"
          : "后端控制面未连接，请确认后端进程已启动。",
        elapsedMs: health.elapsedMs,
      },
      {
        id: "mic",
        label: "麦克风",
        icon: Mic,
        status: "unknown",
        result: "未检测",
        detail: "麦克风采集在手机端，桌面后端无实时探测信号。",
        elapsedMs: null,
      },
      {
        id: "playback",
        label: "播放",
        icon: Volume2,
        status: "unknown",
        result: "未检测",
        detail: "语音播放发生在手机端，桌面后端无实时探测信号。",
        elapsedMs: null,
      },
      {
        id: "sidecar",
        label: "语音组件（sidecar）",
        icon: Box,
        status: sidecar === "running" ? "ok" : sidecar === "stopped" ? "error" : "unknown",
        result: sidecar === "running" ? "正常" : sidecar === "stopped" ? "异常" : "未检测",
        detail:
          sidecar === "running"
            ? "语音组件运行中。"
            : sidecar === "stopped"
              ? "语音组件未运行，请重启应用或联系支持。"
              : "开发态无 Tauri 运行时，无法读取语音组件状态。",
        elapsedMs: sidecarElapsed || null,
      },
    ];

    setItems(next);
    setLastRunAt(Date.now());
    setProgress(null);
    setRunning(false);
  };

  useEffect(() => {
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="diag-panel" role="dialog" aria-label="运行诊断">
      <div className="dg-head">
        <button type="button" className="dg-back" onClick={onBack} aria-label="返回设置">
          <ChevronLeft size={16} strokeWidth={2} aria-hidden="true" />
        </button>
        <span className="dg-title">运行诊断</span>
        <button type="button" className="dg-close" onClick={onClose} aria-label="关闭诊断">
          <X size={14} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>

      <div className="dg-toolbar">
        <button type="button" className="dg-run" onClick={() => void run()} disabled={running}>
          {running ? (
            <Loader2 size={13} strokeWidth={2} className="dg-spin" aria-hidden="true" />
          ) : (
            <RefreshCw size={13} strokeWidth={2} aria-hidden="true" />
          )}
          <span>{running ? "检测中…" : "重新检测"}</span>
        </button>
        {lastRunAt && !running && (
          <span className="dg-last mono">
            最近检查 {new Date(lastRunAt).toLocaleTimeString("zh-CN", { hour12: false })}
          </span>
        )}
      </div>

      {running && progress && (
        <div className="dg-progress" role="status" aria-live="polite">
          <Loader2 size={13} strokeWidth={2} className="dg-spin" aria-hidden="true" />
          <span>
            正在检查 {progress.current}（{progress.done}/6）
          </span>
        </div>
      )}

      {!running && items.length === 0 && (
        <div className="dg-empty">尚未运行诊断</div>
      )}

      {!running && items.length > 0 && (
        <ul className="dg-list">
          {items.map((item) => {
            const Icon = item.icon;
            const meta = statusMeta(item.status);
            const StatusIcon = meta.icon;
            const expanded = expandedId === item.id;
            return (
              <li key={item.id} className="dg-item">
                <button
                  type="button"
                  className="dg-row"
                  aria-expanded={expanded}
                  onClick={() => setExpandedId(expanded ? null : item.id)}
                >
                  <Icon size={14} strokeWidth={2} className="dg-item-icon" aria-hidden="true" />
                  <span className="dg-item-label">{item.label}</span>
                  <StatusIcon
                    size={14}
                    strokeWidth={2}
                    className={`dg-status dg-tone-${meta.tone}`}
                    aria-hidden="true"
                  />
                  <span className={`dg-result dg-tone-${meta.tone}`}>{meta.result}</span>
                  {item.elapsedMs != null && (
                    <span className="dg-elapsed mono">{item.elapsedMs}ms</span>
                  )}
                </button>
                {expanded && <p className="dg-detail">{item.detail}</p>}
              </li>
            );
          })}
        </ul>
      )}

      <style>{`
        .diag-panel {
          background: var(--surface);
          border: 1px solid var(--border);
          border-radius: 12px;
          width: 300px;
          font-size: 13px;
          box-shadow: var(--elev-modal);
          overflow: hidden;
        }
        .dg-head {
          display: flex; align-items: center; gap: 8px;
          padding: 10px 14px;
          border-bottom: 1px solid var(--border-soft);
        }
        .dg-title {
          flex: 1;
          font-family: var(--font-display);
          font-weight: var(--weight-announce);
          font-size: 14px;
        }
        .dg-back, .dg-close {
          display: inline-flex; align-items: center; justify-content: center;
          width: var(--target-min); height: var(--target-min);
          border: none; border-radius: 6px;
          background: transparent; color: var(--muted); cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard), color var(--motion-fast) var(--ease-standard);
        }
        .dg-back:hover, .dg-close:hover { background: var(--surface-raised); color: var(--fg); }
        .dg-back:focus-visible, .dg-close:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .dg-toolbar {
          display: flex; align-items: center; gap: 10px;
          padding: 10px 14px;
          border-bottom: 1px solid var(--border-soft);
        }
        .dg-run {
          display: inline-flex; align-items: center; gap: 6px;
          min-height: 32px; padding: 0 12px;
          background: var(--button-primary-bg); color: var(--button-primary-fg);
          border: none; border-radius: var(--radius-md);
          font-size: 12px; font-weight: var(--weight-emphasize); cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .dg-run:hover:not(:disabled) { background: var(--button-primary-hover); }
        .dg-run:disabled { opacity: 0.6; cursor: default; }
        .dg-run:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .dg-last { font-size: 11px; color: var(--muted); }
        .dg-progress {
          display: flex; align-items: center; gap: 6px;
          padding: 10px 14px; font-size: 12px; color: var(--fg-2);
          border-bottom: 1px solid var(--border-soft);
        }
        .dg-empty { padding: 16px 14px; color: var(--muted); font-size: 12px; }
        .dg-list { list-style: none; }
        .dg-item { border-bottom: 1px solid var(--border-soft); }
        .dg-item:last-child { border-bottom: none; }
        .dg-row {
          display: flex; align-items: center; gap: 8px;
          width: 100%; padding: 10px 14px;
          border: none; background: transparent;
          color: var(--fg); cursor: pointer;
          font-size: 13px; font-family: var(--font-body);
          text-align: left;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .dg-row:hover { background: var(--surface-raised); }
        .dg-row:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: -2px; }
        .dg-item-icon { color: var(--muted); flex: none; }
        .dg-item-label { flex: 1; }
        .dg-result { font-size: 12px; }
        .dg-elapsed { font-size: 11px; color: var(--muted); }
        .dg-tone-ok { color: var(--success); }
        .dg-tone-error { color: var(--danger); }
        .dg-tone-unknown { color: var(--warn); }
        .dg-detail {
          padding: 0 14px 10px 36px;
          font-size: 12px; line-height: var(--leading-body); color: var(--fg-2);
        }
        .dg-spin { animation: dg-spin 0.8s linear infinite; }
        @keyframes dg-spin { to { transform: rotate(360deg); } }
        @media (prefers-reduced-motion: reduce) {
          .dg-spin { animation: none; }
        }
      `}</style>
    </div>
  );
}
