'use strict';

// 手机端 → sidecar 的 TRTC 自定义命令（sendCustomCmdMsg）终止通知处理（Task #23 上游接线）。
//
// 链路：Android RtcClient.sendTerminationNotice(tid)（cmdId=1, reliable=true, ordered=true）
//       → onRecvCustomCmdMsg → 本模块解析 → bridge.noteTermination(currentSessionId, tid)
//       → 既有 bridge WS ctrl 上行 → rtc_bridge 注入注册表（drain 后上报 bridge_drained_closed）。
//
// fail-safe：非 cmdId=1 / bridge 缺失 / 畸形 JSON / 非 note_termination / 非法 tid
//            一律静默忽略，绝不影响通话主链路。sessionId 合法性由
//            BridgeClient.noteTermination 自校验（空/非法返回 false 不发送），
//            因此「对端离开后 currentSessionId 已清空」时本处理器天然退化为 no-op。

/** 与 Android RtcClient.CMD_ID_TERMINATE 对齐的命令 ID */
const CMD_ID_TERMINATE = 1;

function toUtf8(message) {
  if (typeof message === 'string') return message;
  return Buffer.from(message).toString('utf8');
}

/**
 * 构造 onRecvCustomCmdMsg 处理器（纯依赖注入，可在 node --test 中脱离 Electron 单测）。
 *
 * @param {() => object|null} getBridge 取当前 BridgeClient（可能为 null）
 * @param {() => string|null} getCurrentSessionId 取当前会话 ID（pollAndJoin 成功后设置、对端离开/退出时清空）
 * @param {(tag: string, msg: string) => void} log 日志函数（logger 注入）
 * @returns {(userId: string, cmdId: number, seq: number, message: ArrayBuffer|Uint8Array|string) => void}
 */
function makeTerminationCmdHandler(getBridge, getCurrentSessionId, log) {
  return function onTerminationCustomCmd(userId, cmdId, seq, message) {
    if (cmdId !== CMD_ID_TERMINATE) {
      log('CTRL', `忽略非终止自定义命令 cmdId=${cmdId} userId=${userId} seq=${seq}`);
      return;
    }
    const target = getBridge();
    if (!target) return;
    let payload;
    try {
      payload = JSON.parse(toUtf8(message));
    } catch (_) {
      log('CTRL', '终止通知消息非合法 JSON，忽略');
      return;
    }
    if (!payload || payload.type !== 'note_termination') return;
    const tid = payload.termination_id;
    if (typeof tid !== 'string' || !tid || tid.length > 128) {
      log('CTRL', '终止通知 termination_id 非法，忽略');
      return;
    }
    try {
      const relayed = target.noteTermination(getCurrentSessionId(), tid);
      log('CTRL', `收到手机端终止通知 tid=${tid} relayed=${relayed}`);
    } catch (_) {
      // fail-safe：中继失败绝不影响 RTC 通话主链路
    }
  };
}

module.exports = { CMD_ID_TERMINATE, makeTerminationCmdHandler };
