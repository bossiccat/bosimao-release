'use strict';

// intent-selection.js —— pending 会话意图选择（纯函数，无 IO）。
//
// 契约：test/startup-session-contract.test.js
//   「pending intent selection skips the current room and supports multiple devices」
//   - 跳过 room_id === currentRoom 的意图（已在房间不重复加入/不选自己）；
//   - 按列表顺序返回第一个可选意图（支持多设备 pending 并存）；
//   - 无可选意图返回 null（pollAndJoin 跳过本轮轮询）。
//
// 不在此处校验 claim_token 等签发字段——那是 /session/sign 的消费契约
//（fetchSigForDevice），此处只做选择；多余校验会把控制面契约错误放大成本地拒绝。

function selectPendingIntent(intents, currentRoom) {
  if (!Array.isArray(intents)) return null;
  for (const intent of intents) {
    if (!intent || typeof intent !== 'object' || !intent.room_id) continue;
    if (currentRoom && intent.room_id === currentRoom) continue;
    return intent;
  }
  return null;
}

module.exports = { selectPendingIntent };
