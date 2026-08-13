/**
 * 宠物语音体验状态机（XState v5）— 跨端一致 10 态（Task 8 / SPEC §4.2）
 *
 * 体验状态与 Android VoiceUiModel.ExperienceState 同名枚举严格一致：
 * idle / requesting_permission / connecting / listening / endpointing /
 * thinking / speaking / interrupted / recovering / error
 *
 * 事件来源（App.tsx 由 WS /ws/pet 映射）：
 * - SPEECH_START  ← pet_state=listening（用户说话中）
 * - SPEECH_END    ← pet_state=thinking（输入结束）
 * - RESPONSE_START← pet_state=speaking（响应开始）
 * - RESPONSE_END  ← pet_state=monitoring（响应结束回落 listening）
 * - TIMEOUT       ← pet_state=idle（静默超时回落 idle）
 * - BARGE_IN      ← 打断（speaking → interrupted → listening）
 * - ALERT / ALERT_DISMISS ← 四级打扰提醒（独立维度，不改变语音状态机主流转）
 */
import { createMachine, assign } from "xstate";

export type PetState =
  | "idle"
  | "requesting_permission"
  | "connecting"
  | "listening"
  | "endpointing"
  | "thinking"
  | "speaking"
  | "interrupted"
  | "recovering"
  | "error";

interface PetContext {
  alertLevel: number;
  alertAppId: string;
  transcript: string;
}

export const petMachine = createMachine(
  {
    id: "pet",
    initial: "idle",
    // 通过 types 消费 PetContext（避免 TS6196 未使用接口）
    types: {} as { context: PetContext },
    context: {
      alertLevel: 0,
      alertAppId: "",
      transcript: "",
    },
    states: {
      idle: {
        on: {
          START: { target: "connecting" },
          PERMISSION_DENIED: { target: "requesting_permission" },
          ERROR: { target: "error" },
          ALERT: { target: "idle", actions: "setAlert" },
          ALERT_DISMISS: { target: "idle" },
        },
      },
      requesting_permission: {
        on: {
          PERMISSION_GRANTED: { target: "idle" },
          OPEN_SETTINGS: { target: "requesting_permission" },
          ALERT_DISMISS: { target: "requesting_permission" },
        },
      },
      connecting: {
        on: {
          CONNECTED: { target: "listening" },
          CANCEL: { target: "idle" },
          ERROR: { target: "error" },
          TIMEOUT: { target: "error" },
          ALERT: { target: "connecting", actions: "setAlert" },
          ALERT_DISMISS: { target: "connecting" },
        },
      },
      listening: {
        on: {
          SPEECH_END: { target: "endpointing" },
          SPEECH_START: { target: "listening" }, // 连续说话不重置
          RESPONSE_START: { target: "speaking" },
          BARGE_IN: { target: "listening" },
          TIMEOUT: { target: "idle" }, // 静默超时回落
          CANCEL: { target: "idle" },
          ERROR: { target: "error" },
          ALERT: { target: "listening", actions: "setAlert" },
          ALERT_DISMISS: { target: "listening" },
        },
      },
      endpointing: {
        on: {
          RESPONSE_START: { target: "thinking" },
          SPEECH_START: { target: "listening" },
          BARGE_IN: { target: "listening" },
          TIMEOUT: { target: "idle" },
          ALERT: { target: "endpointing", actions: "setAlert" },
          ALERT_DISMISS: { target: "endpointing" },
        },
      },
      thinking: {
        on: {
          RESPONSE_START: { target: "speaking" },
          SPEECH_START: { target: "listening" }, // barge-in：打断响应重新收听
          BARGE_IN: { target: "listening" },
          CANCEL: { target: "idle" },
          ERROR: { target: "error" },
          ALERT: { target: "thinking", actions: "setAlert" },
          ALERT_DISMISS: { target: "thinking" },
        },
      },
      speaking: {
        on: {
          RESPONSE_END: { target: "listening" }, // 正常回复结束回 listening（订阅长期有效）
          SPEECH_START: { target: "listening" },
          BARGE_IN: { target: "interrupted" }, // 显式打断先入 interrupted
          CANCEL: { target: "idle" },
          ERROR: { target: "error" },
          ALERT: { target: "speaking", actions: "setAlert" },
          ALERT_DISMISS: { target: "speaking" },
        },
      },
      interrupted: {
        on: {
          RESUME: { target: "listening" }, // 打断后自动回 listening（外部/上层驱动）
          SPEECH_START: { target: "listening" },
          ERROR: { target: "error" },
          ALERT: { target: "interrupted", actions: "setAlert" },
          ALERT_DISMISS: { target: "interrupted" },
        },
      },
      recovering: {
        on: {
          RETRY: { target: "connecting" },
          RECOVERED: { target: "listening" },
          ERROR: { target: "error" },
          ALERT: { target: "recovering", actions: "setAlert" },
          ALERT_DISMISS: { target: "recovering" },
        },
      },
      error: {
        on: {
          RETRY: { target: "connecting" },
          DISMISS: { target: "idle" },
          ALERT_DISMISS: { target: "error" },
          ALERT: { target: "error", actions: "setAlert" },
        },
      },
    },
  },
  {
    actions: {
      setAlert: assign({
        alertLevel: ({ event }) => (event as { data?: { level?: number } }).data?.level ?? 1,
        alertAppId: ({ event }) => (event as { data?: { app_id?: string } }).data?.app_id ?? "",
      }),
    },
  }
);
