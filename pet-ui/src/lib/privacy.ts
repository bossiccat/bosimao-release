/**
 * 隐私开关 API 客户端（ADR-021 D2 契约，阶段 B-2）
 *
 * - GET  /api/v1/privacy           读全部开关（owner/device/sidecar 任一有效主体）
 * - PATCH /api/v1/privacy/{setting} 设单开关（owner Bearer + X-Request-Nonce + 限流）
 *
 * 信任红线：PATCH 失败必须回滚 UI（调用方据此回滚本地开关值），绝不做成「假开关」。
 * owner 凭证复用后端 voice_owner_credential（项目 owner token），经 Tauri 命令
 * `get_owner_credential` 读取后只进入本次请求的 Authorization 头，不落任何前端状态。
 */
import { invoke } from "@tauri-apps/api/core";

export const PRIVACY_API_BASE = "https://127.0.0.1:8000/api/v1/privacy";

export interface PrivacySettings {
  cloud_processing_enabled: boolean;
  microphone_enabled: boolean;
  background_conversation_enabled: boolean;
  desktop_capture_enabled: boolean;
  transcript_persistence_enabled: boolean;
}

export type PrivacySettingKey = keyof PrivacySettings;

/** 路径取值（ADR-021 D2）：cloud_processing / microphone / background_conversation / desktop_capture / transcript_persistence */
export type PrivacySettingPath =
  | "cloud_processing"
  | "microphone"
  | "background_conversation"
  | "desktop_capture"
  | "transcript_persistence";

export const KEY_TO_PATH: Record<PrivacySettingKey, PrivacySettingPath> = {
  cloud_processing_enabled: "cloud_processing",
  microphone_enabled: "microphone",
  background_conversation_enabled: "background_conversation",
  desktop_capture_enabled: "desktop_capture",
  transcript_persistence_enabled: "transcript_persistence",
};

interface ApiEnvelope<T> {
  code: number;
  data: T;
  message: string;
}

export interface PrivacyPatchData {
  setting: string;
  applied_at: number;
  effective_value: boolean;
  action_result: "ok" | "failed";
}

export class PrivacyApiError extends Error {
  code: number;
  constructor(code: number, message: string) {
    super(message || "隐私设置请求失败");
    this.code = code;
  }
}

/** 读取 owner 凭证（复用项目 owner token）。Tauri 未实现 / 开发态返回 null → 请求无 Authorization，后端 fail-closed 40101。 */
export async function getOwnerToken(): Promise<string | null> {
  try {
    return await invoke<string>("get_owner_credential");
  } catch {
    return null;
  }
}

/** 一次性 nonce（防重放）：≥16 字符随机串，每次写请求新生成（ADR-021 D2 / SPEC §9.1）。 */
export function freshNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
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

  const res = await fetch(`${PRIVACY_API_BASE}${path}`, { ...init, headers });
  const body = (await res.json().catch(() => null)) as ApiEnvelope<unknown> | null;
  if (!res.ok || !body || body.code !== 0) {
    throw new PrivacyApiError(body?.code ?? res.status, body?.message ?? `HTTP ${res.status}`);
  }
  return body.data as T;
}

export async function fetchPrivacySettings(): Promise<PrivacySettings> {
  const data = await request<{ settings: PrivacySettings }>("");
  return data.settings;
}

export async function setPrivacySetting(
  path: PrivacySettingPath,
  enabled: boolean
): Promise<PrivacyPatchData> {
  return request<PrivacyPatchData>(`/${path}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });
}
