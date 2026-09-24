/**
 * verifier 面板验证 harness（非产品代码）：直接挂载各面板组件出图，供独立复核取证。
 * 用法：panels.html?panel=settings|monitor|ca|diag|privacy|error|conn|all
 * 说明：桌面端面板仅在「设置→子面板」内渲染，headless 无交互打不开，故用此页直接挂载。
 * 为在无后端环境下展示真实内容，本页在加载前覆盖 window.fetch 返回样例数据
 *（仅 harness 行为，不修改任何产品文件）。
 */
import { MonitorPanel, type SessionData } from "../src/components/MonitorPanel";
import { Settings, type MonitorTarget, type SettingsView } from "../src/components/Settings";
import { CaConfirm } from "../src/components/CaConfirm";
import { Diagnostics } from "../src/components/Diagnostics";
import { ErrorBanner, type Fault } from "../src/components/ErrorBanner";
import { ConnectionBadge, type VoiceConnPhase } from "../src/components/ConnectionBadge";
import { PrivacySettings } from "../src/components/PrivacySettings";
import "../src/styles/global.css";
import "../src/styles/shell.css";
import "../src/styles/pet.css";

// ---- harness-only fetch mock（让隐私/诊断/设备面板在无后端时显示真实内容） ----
function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
const SAMPLE_SETTINGS = {
  cloud_processing_enabled: true,
  microphone_enabled: false,
  background_conversation_enabled: true,
  desktop_capture_enabled: true,
  transcript_persistence_enabled: false,
};
const SAMPLE_DEVICES = [
  {
    device_id: "a1b2c3d4-0000-1111-2222-333344445555",
    device_name: "Pixel 8 Pro",
    platform: "android",
    status: "online",
    expires_at: 1760000000,
    last_seen_at: 1759000000,
    created_at: 1756000000,
  },
];
(globalThis as unknown as { fetch: typeof fetch }).fetch = (async (input: RequestInfo | URL) => {
  const u = String(input);
  if (u.includes("/privacy")) return json({ code: 0, data: { settings: SAMPLE_SETTINGS }, message: "" });
  if (u.includes("/devices")) return json({ code: 0, data: { items: SAMPLE_DEVICES, total: 1, page: 1, limit: 20, hasMore: false }, message: "" });
  if (u.includes("/health")) return json({ status: "ok", model_server: "up" });
  if (u.includes("/status")) return json({ engine: { model_loaded: true, vram_mb: 2048, inference_busy: false } });
  return json({ code: 0, data: {}, message: "" });
}) as typeof fetch;

const SESSIONS: SessionData[] = [
  {
    app_id: "codex", app_name: "OpenAI Codex", window_found: true, capture_mode: "wgc",
    state: "progress", state_changed_at: 1759000000, stuck_frames: 0,
    last_summary: "已实现登录接口，正在补全单元测试", last_suggestion: "继续补全边界用例",
    last_frame_at: 1759000030, frame_count: 42, last_analysis_ms: 312, alert_level: 0,
  },
  {
    app_id: "trae", app_name: "Trae", window_found: true, capture_mode: "wgc",
    state: "stuck", state_changed_at: 1758990000, stuck_frames: 3,
    last_summary: "依赖安装卡在 resolution", last_suggestion: "检查镜像源后重试",
    last_frame_at: 1758990100, frame_count: 18, last_analysis_ms: 405, alert_level: 2,
  },
  {
    app_id: "hermes", app_name: "Hermes", window_found: true, capture_mode: "wgc",
    state: "off_track", state_changed_at: 1758980000, stuck_frames: 0,
    last_summary: "正在调用未授权外部 API", last_suggestion: "停止并回到主线任务",
    last_frame_at: 1758980500, frame_count: 27, last_analysis_ms: 360, alert_level: 3,
  },
];

const TARGETS: MonitorTarget[] = [
  { app_id: "codex", app_name: "OpenAI Codex", enabled: true },
  { app_id: "trae", app_name: "Trae", enabled: true },
  { app_id: "hermes", app_name: "Hermes", enabled: false },
];

const noop = () => {};
const noopNav = (_v: SettingsView) => {};

function PanelView({ panel }: { panel: string }) {
  switch (panel) {
    case "monitor":
      return <MonitorPanel sessions={SESSIONS} />;
    case "settings":
      return (
        <Settings targets={TARGETS} onToggleTarget={noop} onClose={noop} onNavigate={noopNav} />
      );
    case "ca":
      return <CaConfirm onClose={noop} />;
    case "diag":
      return <Diagnostics onBack={noop} onClose={noop} />;
    case "privacy":
      return <PrivacySettings />;
    case "error":
      return (
        <ErrorBanner
          fault={{ category: "voice", reason: "语音链路异常，请检查网络或模型服务后重试", actionLabel: "重启语音", action: "restart-voice" } as Fault}
          onAction={noop}
          onDismiss={noop}
        />
      );
    case "conn":
      return <ConnectionBadge voicePhase={"idle" as VoiceConnPhase} />;
    default:
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 24, alignItems: "center" }}>
          <Settings targets={TARGETS} onToggleTarget={noop} onClose={noop} onNavigate={noopNav} />
          <MonitorPanel sessions={SESSIONS} />
          <CaConfirm onClose={noop} />
          <Diagnostics onBack={noop} onClose={noop} />
          <PrivacySettings />
          <ErrorBanner
            fault={{ category: "ws", reason: "与后端控制面断开，正在自动重连", actionLabel: "立即重连", action: "reconnect" } as Fault}
            onAction={noop}
            onDismiss={noop}
          />
          <ConnectionBadge voicePhase={"session" as VoiceConnPhase} />
        </div>
      );
  }
}

function readPanel(): string {
  const q = new URLSearchParams(window.location.search);
  return q.get("panel") || "all";
}

export function PanelsBoard() {
  const panel = readPanel();
  return (
    <div
      style={{
        minHeight: "100vh",
        boxSizing: "border-box",
        padding: 24,
        background: "#cfd4d2",
        display: "flex",
        justifyContent: panel === "ca" || panel === "error" || panel === "conn" ? "center" : "flex-start",
        alignItems: panel === "ca" ? "center" : "flex-start",
      }}
    >
      <PanelView panel={panel} />
    </div>
  );
}

const root = document.getElementById("root");
if (root) {
  import("react-dom/client").then(({ createRoot }) => {
    import("react").then((React) => {
      createRoot(root).render(<React.StrictMode><PanelsBoard /></React.StrictMode>);
    });
  });
}
