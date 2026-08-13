/**
 * 设备管理 API 客户端（阶段 C · UI 第二波）— SPEC §5 设备端点
 *
 * - GET  /api/v1/voice/devices                 已注册设备列表（owner Bearer + 限流）
 * - POST /api/v1/voice/devices/{id}/revoke     撤销设备（owner Bearer + X-Request-Nonce + 限流）
 *
 * 信任红线：owner 凭证取不到时请求不带 Authorization，后端 fail-closed 40101；
 * 调用方据此禁用撤销操作，绝不做「假按钮」。凭证经 Tauri `get_owner_credential`
 * 读取后只进入本次请求头，不落任何前端状态（复用 privacy.ts 的同一 helper）。
 */
import { freshNonce, getOwnerToken } from "./privacy";

export const VOICE_API_BASE = "https://127.0.0.1:8000/api/v1/voice";

export interface Device {
  device_id: string;
  device_name: string;
  platform: string;
  status: string;
  expires_at: number;
  last_seen_at: number | null;
  created_at: number;
}

interface DeviceListData {
  items: Device[];
  total: number;
  page: number;
  limit: number;
  hasMore: boolean;
}

interface ApiEnvelope<T> {
  code: number;
  data: T;
  message: string;
}

export class DeviceApiError extends Error {
  code: number;
  constructor(code: number, message: string) {
    super(message || "设备请求失败");
    this.code = code;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = await getOwnerToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (init?.method && init.method !== "GET") {
    headers["X-Request-Nonce"] = freshNonce();
  }

  const res = await fetch(`${VOICE_API_BASE}${path}`, { ...init, headers });
  const body = (await res.json().catch(() => null)) as ApiEnvelope<unknown> | null;
  if (!res.ok || !body || body.code !== 0) {
    throw new DeviceApiError(body?.code ?? res.status, body?.message ?? `HTTP ${res.status}`);
  }
  return body.data as T;
}

export async function fetchDevices(): Promise<Device[]> {
  const data = await request<DeviceListData>("/devices");
  return data.items ?? [];
}

export async function revokeDevice(deviceId: string, reason: string): Promise<void> {
  await request(`/devices/${encodeURIComponent(deviceId)}/revoke`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}
