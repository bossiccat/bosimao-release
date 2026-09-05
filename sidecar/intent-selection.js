'use strict';

// intent-selection.js —— pending 会话意图选择（纯函数，无 IO）。
//
// 契约：test/startup-session-contract.test.js
//   「pending intent selection skips the current room and supports multiple devices」
//   - 跳过 room_id === currentRoom 的意图（已在房间不重复加入/不选自己）；
//   - 按列表顺序返回第一个可选意图（支持多设备 pending 并存）；
//   - 无可选意图返回 null（pollAndJoin 跳过本轮轮询）。
// 兑付失败自愈（2026-09-05，test/redemption-recovery.test.js）：
//   - 第三参 failedSessions（可选，需支持 .has）：跳过已判死的 session_id，
//     防止轮询反复选中已消费意图 → 重签 40914 死循环。
//
// 不在此处校验 claim_token 等签发字段——那是 /session/sign 的消费契约
//（fetchSigForDevice），此处只做选择；多余校验会把控制面契约错误放大成本地拒绝。

function selectPendingIntent(intents, currentRoom, failedSessions) {
  if (!Array.isArray(intents)) return null;
  for (const intent of intents) {
    if (!intent || typeof intent !== 'object' || !intent.room_id) continue;
    if (currentRoom && intent.room_id === currentRoom) continue;
    if (failedSessions && failedSessions.has &&
        failedSessions.has(intent.session_id)) continue;
    return intent;
  }
  return null;
}

module.exports = { selectPendingIntent };
