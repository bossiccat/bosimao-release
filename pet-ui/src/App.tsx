/** 主入口：宠物 + 面板 + WS 状态驱动 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useActor } from "@xstate/react";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { Pet } from "./components/Pet";
import { VoiceOrb, type VoicePhase } from "./components/VoiceOrb";
import { MonitorPanel, type SessionData } from "./components/MonitorPanel";
import { ReminderToast, type AlertData } from "./components/ReminderToast";
import { Settings as SettingsPanel, type MonitorTarget, type SettingsView } from "./components/Settings";
import { Diagnostics } from "./components/Diagnostics";
import { About } from "./components/About";
import { ControlDock } from "./components/ControlDock";
import { toVoicePhase } from "./components/ConnectionBadge";
import { ErrorBanner, type Fault } from "./components/ErrorBanner";
import { CaConfirm } from "./components/CaConfirm";
import { petMachine, type PetState } from "./state/petMachine";
import { wsClient } from "./state/wsClient";
import "./styles/global.css";
import "./styles/shell.css";

export default function App() {
  const [snapshot, send] = useActor(petMachine);
  const [sessions, setSessions] = useState<SessionData[]>([]);
  const [alert, setAlert] = useState<AlertData | null>(null);
  const [showPanel, setShowPanel] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [settingsView, setSettingsView] = useState<SettingsView>("main");
  const [fault, setFault] = useState<Fault | null>(null);
  const [showCaConfirm, setShowCaConfirm] = useState(false);
  const [hovered, setHovered] = useState(false);
  const wsFaultedRef = useRef(false);

  useEffect(() => {
    if (!isTauri()) return; // vite dev 浏览器环境无 Tauri IPC，跳过
    let disposed = false;
    let unlisten: (() => void) | undefined;
    const show = () => {
      if (!disposed) setShowCaConfirm(true);
    };
    listen("ca-confirm-required", show).then((fn) => {
      if (disposed) fn();
      else unlisten = fn;
    });
    invoke<boolean>("is_ca_install_required")
      .then((required) => {
        if (required) show();
      })
      .catch(() => {});
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    const off = wsClient.on((evt) => {
      if (evt.type === "event") {
        if (evt.event === "session_updated") {
          const data = evt.data as SessionData;
          setSessions((prev) => {
            const idx = prev.findIndex((s) => s.app_id === data.app_id);
            if (idx === -1) return [...prev, data];
            const next = [...prev];
            next[idx] = data;
            return next;
          });
        } else if (evt.event === "alert") {
          const data = evt.data as AlertData;
          // 四级打扰：level 1/2 不动声色（仅状态点变色），仅 ≥3 进入提醒态
          if (data.level >= 3) {
            setAlert(data);
            send({ type: "ALERT", data });
            setShowPanel(true);
          }
        } else if (evt.event === "pet_state") {
          // pet_state 是语音全双工会话的权威状态（backend → UI）
          const state = (evt.data as { state?: string }).state;
          switch (state) {
            case "listening":
              send({ type: "SPEECH_START" });
              break;
            case "thinking":
              send({ type: "SPEECH_END" });
              break;
            case "speaking":
              send({ type: "RESPONSE_START" });
              break;
            case "monitoring":
              send({ type: "RESPONSE_END" });
              break;
            case "idle":
              send({ type: "TIMEOUT" });
              break;
            // 2026-08-13 UI 商业化升级：补 connecting/error/recovering 映射（AC-20）
            case "connecting":
              send({ type: "START" });
              break;
            case "error":
              send({ type: "ERROR" });
              setFault({
                category: "voice",
                reason: "语音链路异常，请检查网络或模型服务后重试",
                actionLabel: "重启语音",
                action: "restart-voice",
              });
              break;
            case "recovering":
              send({ type: "RETRY" });
              break;
            default:
              break;
          }
        }
      } else if (evt.type === "pong") {
        if (wsFaultedRef.current) {
          wsFaultedRef.current = false;
          setFault(null);
        }
      }
    });
    // 控制面断线 → AC-20 分类故障提示（2 秒内可感知）
    const offConn = wsClient.onConn((state) => {
      if (state === "reconnecting" && !wsFaultedRef.current) {
        wsFaultedRef.current = true;
        setFault({
          category: "ws",
          reason: "与后端控制面断开，正在自动重连",
          actionLabel: "立即重连",
          action: "reconnect",
        });
      }
      if (state === "open") {
        wsFaultedRef.current = false;
        setFault(null);
      }
    });
    wsClient.connect();
    return () => {
      off();
      offConn();
      wsClient.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!isTauri()) return;
    let width = 200;
    let height = 200;
    if (showPanel) {
      width = Math.max(width, 380);
      height = Math.max(height, 480);
    }
    if (showSettings) {
      width = Math.max(width, 380);
      height = Math.max(height, 560);
    }
    if (showCaConfirm) {
      width = Math.max(width, 400);
      height = Math.max(height, 580);
    }
    if (fault) {
      width = Math.max(width, 440);
      height = Math.max(height, 220);
    }
    invoke("set_pet_size", { width, height }).catch(() => {});
  }, [showPanel, showSettings, showCaConfirm, fault]);

  const handleHidePet = () => {
    invoke("hide_pet").catch(() => {});
  };

  const handleFaultAction = (action: Fault["action"]) => {
    if (action === "reconnect") wsClient.connect();
    if (action === "open-settings") setShowSettings(true);
    if (action === "restart-voice") wsClient.control("restart_voice");
    setFault(null);
  };

  const machineState = snapshot.value as PetState;
  const isVoice =
    machineState === "listening" || machineState === "thinking" || machineState === "speaking";
  // 提醒为独立维度（Task 8：语音状态机收紧为 10 体验态，alerting 不再作为机器状态）
  const isAlerting = (alert?.level ?? 0) >= 3;
  const tone = alert?.state === "off_track" ? "danger" : alert?.state === "stuck" ? "warn" : "neutral";
  // 三态交互模型（商业化 2026-09-03）：idle 只见波斯猫本体，控件 hover 淡入；
  // 任何面板/横幅打开期间控件保持可见（放大窗口中需可操作）。
  const controlsHidden = !(hovered || showPanel || showSettings || showCaConfirm || fault);

  // 监控目标：与 config/monitors.yaml 对齐（session 到达后以实际 app_name 为准）
  const targets = useMemo<MonitorTarget[]>(() => {
    const known: MonitorTarget[] = [
      { app_id: "codex", app_name: "OpenAI Codex", enabled: true },
      { app_id: "trae", app_name: "Trae", enabled: true },
      { app_id: "hermes", app_name: "Hermes", enabled: true },
    ];
    return known.map((t) => {
      const s = sessions.find((x) => x.app_id === t.app_id);
      return s ? { ...t, app_name: s.app_name } : t;
    });
  }, [sessions]);

  const togglePanel = () => setShowPanel((v) => !v);
  const onAnchorKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      togglePanel();
    }
  };
  const dismissAlert = () => {
    setAlert(null);
    send({ type: "ALERT_DISMISS" });
  };
  const handleToggleTarget = (appId: string, enabled: boolean) => {
    wsClient.control(enabled ? "start_monitoring" : "stop_monitoring", appId);
  };

  return (
    <div
      className="app-root"
      data-tauri-drag-region
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <div
        className={`pet-anchor${controlsHidden ? " pet-anchor--idle" : ""}`}
        role="button"
        tabIndex={0}
        aria-label="打开监控面板"
        onClick={togglePanel}
        onKeyDown={onAnchorKeyDown}
      >
        {isVoice ? (
          <VoiceOrb phase={machineState as VoicePhase} tone={tone} volume={0.5} />
        ) : (
          <Pet
            mode={isAlerting ? "alerting" : "monitoring"}
            tone={tone}
            sizePx={isAlerting ? 176 : 152}
            opacity={isAlerting ? 1 : 0.9}
            alertPulse={isAlerting}
          />
        )}
      </div>

      {showPanel && (
        <div className="panel-slot" onClick={(e) => e.stopPropagation()}>
          <MonitorPanel sessions={sessions} />
        </div>
      )}

      {alert && alert.level >= 3 && (
        <ReminderToast alert={alert} onDismiss={dismissAlert} />
      )}

      <ControlDock
        controlsHidden={controlsHidden}
        voicePhase={toVoicePhase(machineState)}
        onToggleSettings={() => {
          setShowSettings((v) => !v);
          setSettingsView("main");
        }}
        onHidePet={handleHidePet}
      />

      {showSettings && (
        <div className="settings-slot" onClick={(e) => e.stopPropagation()}>
          {settingsView === "main" && (
            <SettingsPanel
              targets={targets}
              onToggleTarget={handleToggleTarget}
              onClose={() => setShowSettings(false)}
              onNavigate={(view) => setSettingsView(view)}
            />
          )}
          {settingsView === "diagnostics" && (
            <Diagnostics
              onBack={() => setSettingsView("main")}
              onClose={() => setShowSettings(false)}
            />
          )}
          {settingsView === "about" && (
            <About onBack={() => setSettingsView("main")} onClose={() => setShowSettings(false)} />
          )}
        </div>
      )}

      {fault && (
        <ErrorBanner
          fault={fault}
          onAction={handleFaultAction}
          onDismiss={() => setFault(null)}
        />
      )}

      {showCaConfirm && <CaConfirm onClose={() => setShowCaConfirm(false)} />}
    </div>
  );
}
