// 会话意图协调（v1.1：NoSQL voice_intents 存储 —— 修复多实例内存态意图丢失）
//
// 背景：v1.0 用函数实例内存 dict 存意图，SCF 多实例下意图分散丢失（高压测试 S3 实锤：
// 12 路唤醒 pending 只看到 3 个）。v1.1 改用 CloudBase NoSQL collection voice_intents：
//   _id = device_id
//   { room_id: string, ts: number(epoch ms), consumed: boolean, consumed_by?: string }
// 多实例共享数据库，意图全局可见；sign 用条件更新（consumed:false → true）防重复消费。
'use strict';

const config = require('./config');

let _db = null;
function db() {
  if (_db) return _db;
  // 普通事件云函数环境免鉴权；HTTP 访问服务触发需 API Key（见 README）
  const tcb = require('@cloudbase/node-sdk');
  const app = tcb.init({
    env: tcb.SYMBOL_DEFAULT_ENV,
    ...(process.env.CLOUDBASE_APIKEY ? { accessKey: process.env.CLOUDBASE_APIKEY } : {}),
  });
  _db = app.database();
  return _db;
}

const COLL = 'voice_intents';

function roomOf(deviceId) {
  return config.roomPrefix + deviceId;
}

function isFresh(ts) {
  return Date.now() - ts <= config.intentFreshS * 1000;
}

/** 手机唤醒：记录会话意图（upsert 幂等；同设备重复唤醒刷新保鲜） */
async function issue(deviceId) {
  const doc = { room_id: roomOf(deviceId), ts: Date.now(), consumed: false };
  await db().collection(COLL).doc(deviceId).set(doc);
  return doc.room_id;
}

/** PC 轮询：枚举全部未消费且未过期的意图（多实例共享，全局一致） */
async function listPending() {
  const res = await db().collection(COLL).where({ consumed: false }).limit(100).get();
  const out = [];
  const cleanup = [];
  for (const it of res.data || []) {
    if (!isFresh(it.ts)) {
      cleanup.push(it._id);
      continue;
    }
    out.push({ device_id: it._id, room_id: it.room_id, ts: it.ts });
  }
  if (cleanup.length) {
    // 过期意图清理（尽力而为）
    for (const id of cleanup) {
      try {
        await db().collection(COLL).doc(id).remove();
      } catch (_e) { /* 忽略并发删除 */ }
    }
  }
  return out;
}

/**
 * PC sidecar 取签：条件更新 consumed:false → true，防多 PC/重复轮询重复进房。
 * @returns {Promise<{room_id: string, user_id: string} | null>} null = 无有效意图（已被消费/不存在/过期）
 */
async function consume(deviceId, userId) {
  const up = await db().collection(COLL)
    .where({ _id: deviceId, consumed: false })
    .update({ consumed: true, consumed_by: userId, consumed_at: Date.now() });
  // SDK 返回 { updated: number }（顶层字段，非 stats.updated——v1.1 曾误读 stats 导致
  // 数据库已消费但接口误报 40401，PC 不跟进进房，压测 S3 实锤）
  if (!up || Number(up.updated) !== 1) return null;
  return { room_id: roomOf(deviceId), user_id: userId };
}

module.exports = { issue, listPending, consume, roomOf, _setDbForTest: (d) => { _db = d; } };
