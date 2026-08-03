/**
 * 宠物六态状态机（XState v5）— 与 docs/DESIGN.md §4 契约一致
 *
 * 状态：Idle / Monitoring / Listening / Thinking / Speaking / Alerting
 * 事件：WAKE / SPEECH_START / SPEECH_END / RESPONSE_START / RESPONSE_END /
 *       ALERT / ALERT_DISMISS / BARGE_IN / TIMEOUT
 *
 * 事件来源（App.tsx 由 WS /ws/pet 映射）：
 * - SPEECH_START  ← pet_state=listening（voice_wake 唤醒）
 * - SPEECH_END    ← pet_state=thinking（输入结束进入思考）
 * - RESPONSE_START← pet_state=speaking（响应开始）
 * - RESPONSE_END  ← pet_state=monitoring / idle（响应结束回落）
 * - ALERT         ← alert 且 level >= 3（四级打扰，低级别不动声色）
 * - ALERT_DISMISS ← 提醒气泡手动关闭 / 8s 自动消失
 * - BARGE_IN      ← 打断（后端重发 pet_state=listening 或显式打断）
 * - TIMEOUT       ← 静默超时回落 Monitoring
 */
import { createMachine, assign } from "xstate";

export type PetState =
  | "idle"
  | "monitoring"
  | "listening"
  | "thinking"
  | "speaking"
  | "alerting";

interface PetContext {
  alertLevel: number;
  alertAppId: string;
  transcript: string;
}

export const petMachine = createMachine(
  {
    id: "pet",
    initial: "monitoring",
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
          WAKE: { target: "listening" },
          ALERT: { target: "alerting", actions: "setAlert" },
        },
      },
      monitoring: {
        entry: "clearAlert",
        on: {
          WAKE: { target: "listening" },
          SPEECH_START: { target: "listening" },
          ALERT: { target: "alerting", actions: "setAlert" },
        },
      },
      listening: {
        on: {
          SPEECH_END: { target: "thinking" },
          BARGE_IN: { target: "listening" }, // 打断 = 变形不重置（留在本态）
          ALERT: { target: "alerting", actions: "setAlert" },
          TIMEOUT: { target: "monitoring" }, // 静默超时回落
        },
      },
      thinking: {
        on: {
          RESPONSE_START: { target: "speaking" },
          SPEECH_START: { target: "listening" }, // barge-in：后端重发 listening
          BARGE_IN: { target: "listening" },
          ALERT: { target: "alerting", actions: "setAlert" },
        },
      },
      speaking: {
        on: {
          RESPONSE_END: { target: "monitoring" },
          SPEECH_START: { target: "listening" }, // barge-in：打断响应重新收听
          BARGE_IN: { target: "listening" },
          ALERT: { target: "alerting", actions: "setAlert" },
        },
      },
      alerting: {
        on: {
          ALERT_DISMISS: { target: "monitoring" },
          WAKE: { target: "listening" },
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
      clearAlert: assign({
        alertLevel: 0,
        alertAppId: "",
      }),
    },
  }
);
