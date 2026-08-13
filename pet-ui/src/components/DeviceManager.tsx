/**
 * 设备管理区块（阶段 C · UI 第二波）— SPEC §5 / DESIGN §4.5
 *
 * - 列出已注册 Android 设备（device_id 脱敏 / 名称 / 注册时间 / 状态）
 * - 撤销走二次确认：主操作「确认撤销设备」、次操作「保留设备」
 * - owner Bearer 取不到 → fail-closed：停用撤销操作并明示原因，绝不做「假按钮」
 */
import { useEffect, useState } from "react";
import { Loader2, RefreshCw, ShieldX, Smartphone, TriangleAlert } from "lucide-react";
import {
  DeviceApiError,
  fetchDevices,
  revokeDevice,
  type Device,
} from "../lib/devices";
import { getOwnerToken } from "../lib/privacy";

type LoadState = "loading" | "ok" | "error";

/** device_id 中间截断脱敏（DESIGN §5.4 Edge：超长标识中间截断） */
function maskDeviceId(id: string): string {
  if (id.length <= 16) return id;
  return `${id.slice(0, 8)}…${id.slice(-4)}`;
}

function formatTime(epochSeconds: number): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 撤销失败错误码 → 用户可读文案（SPEC §5 错误码锁定表） */
function revokeErrorText(e: unknown): string {
  if (e instanceof DeviceApiError) {
    switch (e.code) {
      case 40101:
        return "无法验证本机身份，请重新登录后重试";
      case 40102:
        return "请求已过期，请重试";
      case 40401:
        return "设备不存在，可能已被撤销";
      case 42901:
        return "操作过于频繁，请稍后重试";
      case 50301:
        return "设备已撤销，但语音会话终止未确认，可稍后重试";
      default:
        return e.message;
    }
  }
  return "后端未连接，无法撤销设备";
}

export function DeviceManager() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [credentialReady, setCredentialReady] = useState<boolean | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const load = async () => {
    setLoadState("loading");
    setErrorMsg(null);
    try {
      // 先探测 owner 凭证是否可用：取不到即 fail-closed，禁用撤销操作
      const token = await getOwnerToken();
      setCredentialReady(token != null);
      const items = await fetchDevices();
      setDevices(items);
      setLoadState("ok");
    } catch {
      setCredentialReady(false);
      setDevices([]);
      setLoadState("error");
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const doRevoke = async (device: Device) => {
    setPendingId(device.device_id);
    setErrorMsg(null);
    try {
      await revokeDevice(device.device_id, "用户主动撤销设备");
      setDevices((list) => list.filter((d) => d.device_id !== device.device_id));
      setConfirmId(null);
    } catch (e) {
      setErrorMsg(revokeErrorText(e));
    } finally {
      setPendingId(null);
    }
  };

  return (
    <div className="dev-mgr" role="group" aria-label="设备管理">
      <div className="dm-head">
        <Smartphone size={14} strokeWidth={2} aria-hidden="true" />
        <span className="dm-head-label">设备管理</span>
        <button
          type="button"
          className="dm-refresh"
          aria-label="刷新设备列表"
          onClick={() => void load()}
          disabled={loadState === "loading"}
        >
          <RefreshCw size={13} strokeWidth={2} aria-hidden="true" />
        </button>
      </div>

      {loadState === "loading" && (
        <div className="dm-state">
          <Loader2 size={13} strokeWidth={2} className="dm-spin" aria-hidden="true" />
          <span>正在读取已注册设备…</span>
        </div>
      )}

      {loadState === "error" && (
        <div className="dm-state dm-state-error" role="alert">
          <TriangleAlert size={13} strokeWidth={2} aria-hidden="true" />
          <span>无法读取设备列表，已停用撤销操作避免误操作。</span>
          <button type="button" className="dm-retry" onClick={() => void load()}>
            重试
          </button>
        </div>
      )}

      {loadState === "ok" && devices.length === 0 && (
        <div className="dm-state">尚未配对手机设备</div>
      )}

      {loadState === "ok" &&
        devices.map((device) => {
          const isConfirm = confirmId === device.device_id;
          const isPending = pendingId === device.device_id;
          return (
            <div key={device.device_id} className="dm-row">
              <div className="dm-row-main">
                <span className="dm-name">{device.device_name || "未命名设备"}</span>
                <span className="dm-meta mono" title={device.device_id}>
                  {maskDeviceId(device.device_id)}
                </span>
                <span className="dm-meta">注册于 {formatTime(device.created_at)}</span>
              </div>

              {!isConfirm ? (
                <button
                  type="button"
                  className="dm-revoke"
                  disabled={!credentialReady || isPending}
                  onClick={() => setConfirmId(device.device_id)}
                >
                  <ShieldX size={13} strokeWidth={2} aria-hidden="true" />
                  <span>撤销</span>
                </button>
              ) : (
                <div className="dm-confirm" role="group" aria-label="确认撤销设备">
                  <p className="dm-confirm-text">
                    撤销后，此设备当前语音会话会立即结束，之后需要重新配对。
                  </p>
                  <div className="dm-confirm-actions">
                    <button
                      type="button"
                      className="dm-confirm-primary"
                      disabled={isPending}
                      onClick={() => void doRevoke(device)}
                    >
                      {isPending ? "撤销中…" : "确认撤销设备"}
                    </button>
                    <button
                      type="button"
                      className="dm-confirm-secondary"
                      disabled={isPending}
                      onClick={() => setConfirmId(null)}
                    >
                      保留设备
                    </button>
                  </div>
                </div>
              )}
            </div>
          );
        })}

      {credentialReady === false && loadState === "ok" && (
        <p className="dm-notice" role="note">
          <TriangleAlert size={12} strokeWidth={2} aria-hidden="true" />
          <span>本机未配置 owner 凭证，设备操作已停用。</span>
        </p>
      )}

      {errorMsg && (
        <p className="dm-error" role="alert">
          <TriangleAlert size={12} strokeWidth={2} aria-hidden="true" />
          <span>{errorMsg}</span>
        </p>
      )}

      <style>{`
        .dev-mgr { display: flex; flex-direction: column; gap: 2px; }
        .dm-head {
          display: flex; align-items: center; gap: 6px;
          color: var(--fg-2); margin-bottom: 6px;
        }
        .dm-head-label {
          font-family: var(--font-mono); font-size: 11px;
          letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted);
          flex: 1;
        }
        .dm-refresh {
          display: inline-flex; align-items: center; justify-content: center;
          width: 24px; height: 24px;
          border: none; border-radius: 6px;
          background: transparent; color: var(--muted); cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard), color var(--motion-fast) var(--ease-standard);
        }
        .dm-refresh:hover:not(:disabled) { background: var(--surface-raised); color: var(--fg); }
        .dm-refresh:disabled { opacity: 0.5; cursor: default; }
        .dm-refresh:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .dm-state {
          display: flex; align-items: center; gap: 6px;
          font-size: 12px; color: var(--fg-2); padding: 6px 0;
        }
        .dm-state-error { color: var(--danger); flex-wrap: wrap; }
        .dm-retry {
          border: 1px solid var(--border); border-radius: var(--radius-sm);
          background: transparent; color: var(--fg);
          font-size: 12px; padding: 2px 8px; cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .dm-retry:hover { background: var(--surface-raised); }
        .dm-retry:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .dm-row {
          display: flex; flex-direction: column; gap: 6px;
          padding: 8px 0;
          border-top: 1px solid var(--border-soft);
        }
        .dm-row:first-of-type { border-top: none; }
        .dm-row-main { display: flex; flex-direction: column; gap: 2px; flex: 1; }
        .dm-name { font-size: 13px; color: var(--fg); font-weight: var(--weight-emphasize); }
        .dm-meta { font-size: 11px; color: var(--muted); }
        .dm-revoke {
          display: inline-flex; align-items: center; gap: 5px;
          align-self: flex-start;
          min-height: 28px; padding: 0 10px;
          background: transparent; color: var(--danger);
          border: 1px solid var(--border); border-radius: 6px;
          font-size: 12px; cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .dm-revoke:hover:not(:disabled) { background: var(--surface-raised); border-color: var(--danger); }
        .dm-revoke:disabled { opacity: 0.5; cursor: default; }
        .dm-revoke:focus-visible { outline: 2px solid var(--focus-ring); outline-offset: 1px; }
        .dm-confirm {
          display: flex; flex-direction: column; gap: 8px;
          padding: 8px;
          background: var(--surface-subtle);
          border: 1px solid var(--border-soft);
          border-radius: var(--radius-md);
        }
        .dm-confirm-text { font-size: 12px; line-height: var(--leading-body); color: var(--fg-2); }
        .dm-confirm-actions { display: flex; gap: 8px; }
        .dm-confirm-primary {
          flex: 1; min-height: 32px;
          display: inline-flex; align-items: center; justify-content: center;
          background: var(--button-danger-bg); color: #fff;
          border: none; border-radius: var(--radius-md);
          font-size: 12px; font-weight: var(--weight-emphasize); cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .dm-confirm-primary:hover:not(:disabled) { filter: brightness(1.08); }
        .dm-confirm-primary:disabled { opacity: 0.6; cursor: default; }
        .dm-confirm-secondary {
          flex: 1; min-height: 32px;
          display: inline-flex; align-items: center; justify-content: center;
          background: var(--button-secondary-bg); color: var(--button-secondary-fg);
          border: 1px solid var(--border); border-radius: var(--radius-md);
          font-size: 12px; cursor: pointer;
          transition: background-color var(--motion-fast) var(--ease-standard);
        }
        .dm-confirm-secondary:hover:not(:disabled) { background: var(--surface-raised); }
        .dm-confirm-secondary:disabled { opacity: 0.6; cursor: default; }
        .dm-confirm-primary:focus-visible, .dm-confirm-secondary:focus-visible {
          outline: 2px solid var(--focus-ring); outline-offset: 1px;
        }
        .dm-notice, .dm-error {
          display: flex; align-items: center; gap: 6px;
          font-size: 12px; color: var(--warn); padding: 4px 0;
        }
        .dm-error { color: var(--danger); }
        .dm-spin { animation: dm-spin 0.8s linear infinite; }
        @keyframes dm-spin { to { transform: rotate(360deg); } }
        @media (prefers-reduced-motion: reduce) {
          .dm-spin { animation: none; }
        }
      `}</style>
    </div>
  );
}
