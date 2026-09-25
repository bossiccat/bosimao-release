/**
 * pet_state（backend → UI 的语音全双工权威状态）→ 宠物状态机事件映射。
 * 从 App.tsx 抽出（App 保持 ≤300 行契约）；2026-08-13 商业化升级含
 * connecting/error/recovering 映射（AC-20）。
 */
export type PetMachineEventType =
  | "SPEECH_START"
  | "SPEECH_END"
  | "RESPONSE_START"
  | "RESPONSE_END"
  | "TIMEOUT"
  | "START"
  | "ERROR"
  | "RETRY";

export function petStateToEvent(state: string | undefined): PetMachineEventType | null {
  switch (state) {
    case "listening":
      return "SPEECH_START";
    case "thinking":
      return "SPEECH_END";
    case "speaking":
      return "RESPONSE_START";
    case "monitoring":
      return "RESPONSE_END";
    case "idle":
      return "TIMEOUT";
    case "connecting":
      return "START";
    case "error":
      return "ERROR";
    case "recovering":
      return "RETRY";
    default:
      return null;
  }
}
