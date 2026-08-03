/**
 * 主入口：宠物 + 监控面板 + WS 状态驱动（六态状态机接线）
 * - 状态机驱动渲染：listening/thinking/speaking → VoiceOrb；monitoring/alerting → Pet
 * - WS 事件 → 状态机事件映射（事件来源注释见 petMachine.ts 头）
 * - 四级打扰：alert level ≥3 才进入 alerting 完整提醒态；level 1/2 不动声色
 *   （仅 MonitorPanel 状态点变色，由 session_updated 的 alert_level 驱动）
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useActor } from "@xstate/react";
import { Settings as SettingsIcon } from "lucide-react";
import { Pet } from "./components/Pet";
import { VoiceOrb, type VoicePhase } from "./components/VoiceOrb";
import { MonitorPanel, type SessionData } from "./components/MonitorPanel";
import { ReminderToast, type AlertData } from "./components/ReminderToast";
import { Settings as SettingsPanel, type MonitorTarget } from "./components/Settings";
import { petMachine, type PetState } from "./state/petMachine";
import { wsClient } from "./state/wsClient";
import "./styles/global.css";

export default function App() {
  const [snapshot, send] = useActor(petMachine);
  const [sessions, setSessions] = useState<SessionData[]>([]);
  const [alert, setAlert] = useState<AlertData | null>(null);
  const [showPanel, setShowPanel] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [wsConnected, setWsConnected] = useState(false);
  const lastPingRef = useRef(0);

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
            default:
              break;
          }
        }
      } else if (evt.type === "pong") {
        lastPingRef.current = Date.now();
        setWsConnected(true);
      }
    });
    wsClient.connect();
    const alive = setInterval(() => {
      if (Date.now() - lastPingRef.current > 45000) setWsConnected(false);
    }, 10000);
    return () => {
      off();
      wsClient.close();
      clearInterval(alive);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const machineState = snapshot.value as PetState;
  const isVoice =
    machineState === "listening" || machineState === "thinking" || machineState === "speaking";
  // 状态机进入 alerting 仅因 level≥3 的 ALERT 事件；再显式兜底一次
  const isAlerting = machineState === "alerting" && (alert?.level ?? 0) >= 3;
  const tone = alert?.state === "off_track" ? "danger" : alert?.state === "stuck" ? "warn" : "neutral";

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
    <div className="app-root">
      <div
        className="pet-anchor"
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
            sizePx={isAlerting ? 140 : 80}
            opacity={isAlerting ? 1 : 0.3}
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

      <button
        type="button"
        className="settings-trigger"
        aria-label="打开设置"
        onClick={() => setShowSettings((v) => !v)}
      >
        <SettingsIcon size={16} strokeWidth={1.8} aria-hidden="true" />
      </button>

      {showSettings && (
        <div className="settings-slot" onClick={(e) => e.stopPropagation()}>
          <SettingsPanel
            targets={targets}
            onToggleTarget={handleToggleTarget}
            onClose={() => setShowSettings(false)}
          />
        </div>
      )}

      {!wsConnected && <div className="ws-badge">后端未连接</div>}

      <style>{`
        .app-root { height: 100vh; position: relative; }
        .pet-anchor { position: fixed; right: 16px; bottom: 16px; z-index: 10; }
        .pet-anchor:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 2px; border-radius: 50%; }
        .panel-slot { position: fixed; right: 16px; bottom: 110px; z-index: 20; }
        .settings-trigger {
          position: fixed; left: 10px; bottom: 10px; z-index: 30;
          display: inline-flex; align-items: center; justify-content: center;
          width: 30px; height: 30px;
          border: 1px solid var(--border); border-radius: 8px;
          background: var(--surface); color: var(--fg-2);
          cursor: pointer;
          transition: background-color var(--motion-fast) ease, color var(--motion-fast) ease;
        }
        .settings-trigger:hover { background: var(--surface-2); color: var(--fg); }
        .settings-trigger:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .settings-slot { position: fixed; left: 10px; bottom: 48px; z-index: 40; }
        .ws-badge {
          position: fixed; left: 10px; top: 10px; z-index: 40;
          background: var(--danger); color: #fff;
          font-size: 11px; padding: 3px 8px; border-radius: 6px;
          font-family: var(--font-mono);
        }
      `}</style>
    </div>
  );
}
