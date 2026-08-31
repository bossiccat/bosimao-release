// 设备配对注册（v1.2：云端商业化闭环 —— 补齐 devices/register + 账户绑定契约）
//
// 数据模型（CloudBase NoSQL）：
//   collection: voice_devices
//     _id = device_id (uuid)
//     { device_name, platform, credential_secret_hash,
//       owner_bound: string|null,   // 绑定的账户标识（扫码绑定后写入）
//       created_at, expires_at, revoked: boolean }
//
//   collection: voice_pairing_codes
//     _id = 随机 32 字符码
//     { platform, device_name_hint, max_uses, used: number,
//       expires_at(epoch s), created_by }
//
// 客户端契约（A6 修复，2026-08-21）：所有返回给客户端的 expires_at 一律
// ISO8601 UTC 字符串（手机端 requiredString 对 number 返回 ""）；
// 存储层继续用 epoch 秒（verifyDeviceCredential 等 TTL 比对逻辑类型不变）
//
// 安全（对齐 PC 后端 routes_voice_devices.py 契约，手机端 DeviceRegistrationApi.kt）：
//   - pairing_code 20-256 字符、TTL 300s、max_uses=1（一次性）
//   - credential_secret 仅注册响应下发一次，云端只存 SHA-256 哈希
//   - 会话签发（index.js handleSession）校验 device 凭证哈希（v1.2 起强制）
'use strict';

const crypto = require('crypto');

const COLL_DEV = 'voice_devices';
const COLL_CODE = 'voice_pairing_codes';

let _db = null;
function db() {
  if (_db) return _db;
  const tcb = require('@cloudbase/node-sdk');
  const app = tcb.init({
    env: tcb.SYMBOL_DEFAULT_ENV,
    ...(process.env.CLOUDBASE_APIKEY ? { accessKey: process.env.CLOUDBASE_APIKEY } : {}),
  });
  _db = app.database();
  return _db;
}

function sha256(s) {
  return crypto.createHash('sha256').update(s, 'utf8').digest('hex');
}

function genSecret() {
  // 43 字符 URL-safe（对齐 PC 端 32..512 契约）
  return crypto.randomBytes(32).toString('base64url');
}

function genCode() {
  return crypto.randomBytes(24).toString('base64url'); // 32 字符
}

/**
 * epoch 秒 → ISO8601 UTC 字符串（客户端契约：手机端 requiredString("expires_at")，
 * number 会被解析为 "" 导致 IOException；云端/PC/手机三端统一 ISO 字符串）
 */
function toIso(epochS) {
  return new Date(epochS * 1000).toISOString();
}

/**
 * Owner/桌面端：创建配对码（展示二维码用）
 * @returns {Promise<{pairing_code, expires_at, max_uses, ttl_seconds}>}
 * expires_at 为 ISO8601 字符串（UTC，如 "2026-08-21T15:00:00.000Z"）
 */
async function createPairingCode(platform, deviceNameHint) {
  const code = genCode();
  const ttl = 300; // 5 分钟
  const doc = {
    platform: platform || 'android',
    device_name_hint: deviceNameHint || '',
    max_uses: 1,
    used: 0,
    expires_at: Math.floor(Date.now() / 1000) + ttl,
    created_at: Math.floor(Date.now() / 1000),
    created_by: 'owner',
  };
  await db().collection(COLL_CODE).doc(code).set(doc);
  return {
    pairing_code: code,
    expires_at: toIso(doc.expires_at), // 契约：客户端字段一律 ISO 字符串；存储层保留 epoch 秒
    max_uses: 1,
    ttl_seconds: ttl,
  };
}

/**
 * 手机：注册设备（凭配对码换取 device_id + credential_secret，一次性）
 * 对齐 PC 端 RegisterDeviceRequest：pairing_code 20-256 / device_name 1-80 / platform=android
 * @returns {Promise<{device_id, credential_id, credential_secret, expires_at}|{error}>}
 */
async function registerDevice(pairingCode, deviceName) {
  const now = Math.floor(Date.now() / 1000);
  const _ = db().command;
  const coll = db().collection(COLL_CODE);
  // 读文档用于过期/状态预检（快速失败 + 兼容旧数据），但消费动作必须原子：
  // 条件更新 where({_id, used < max_uses})，并发双请求只有一个 updated=1（A5 修复，
  // 对齐 signing.js consume() 的 compare-and-update 模式；doc(id).update() 是无条件更新，
  // 文档存在必返回 updated:1，无法防并发重放）
  const codeDoc = await coll.doc(pairingCode).get().catch(() => null);
  const code = codeDoc && codeDoc.data && codeDoc.data[0] !== undefined ? codeDoc.data[0] : (codeDoc && codeDoc.data) || null;
  if (!code || code.used >= code.max_uses || code.expires_at < now) {
    return { error: 'pairing_code_invalid' };
  }
  const claim = await coll
    .where({ _id: pairingCode, used: _.lt(code.max_uses) })
    .update({ used: code.max_uses, used_at: now });
  if (!claim || Number(claim.updated) !== 1) {
    return { error: 'pairing_code_invalid' }; // 并发已被抢（或码不存在）
  }

  const deviceId = crypto.randomUUID();
  const secret = genSecret();
  const expireS = 60 * 60 * 24 * 365; // 1 年
  const devDoc = {
    device_name: deviceName,
    platform: 'android',
    credential_secret_hash: sha256(secret),
    owner_bound: null,
    created_at: now,
    expires_at: now + expireS,
    revoked: false,
  };
  await db().collection(COLL_DEV).doc(deviceId).set(devDoc);
  return {
    device_id: deviceId,
    credential_id: deviceId, // 对齐手机端 RegisteredDevice.credentialId
    credential_secret: secret,
    expires_at: toIso(now + expireS), // 契约：客户端字段 ISO 字符串；存储层保留 epoch 秒
  };
}

/**
 * 校验设备凭证（会话签发时调用）：Bearer 格式 <device_id>.<secret>
 * @returns {Promise<boolean>}
 */
async function verifyDeviceCredential(deviceId, secret) {
  if (!deviceId || !secret) return false;
  const res = await db().collection(COLL_DEV).doc(deviceId).get().catch(() => null);
  const doc = res && res.data && res.data[0] !== undefined ? res.data[0] : (res && res.data) || null;
  if (!doc || doc.revoked) return false;
  if (doc.expires_at && doc.expires_at < Math.floor(Date.now() / 1000)) return false;
  return doc.credential_secret_hash === sha256(secret);
}

/**
 * 扫码账户绑定：把 device 绑到账户（account_id 由登录态提供；MVP 用 owner 标识）
 * @returns {Promise<boolean>} 成功与否
 */
async function bindOwner(deviceId, accountId) {
  if (!deviceId || !accountId) return false;
  const res = await db().collection(COLL_DEV).doc(deviceId).get().catch(() => null);
  const doc = res && res.data && res.data[0] !== undefined ? res.data[0] : (res && res.data) || null;
  if (!doc || doc.revoked) return false;
  if (doc.owner_bound && doc.owner_bound !== accountId) return false; // 已绑他人
  await db().collection(COLL_DEV).doc(deviceId).update({ owner_bound: accountId, bound_at: Math.floor(Date.now() / 1000) });
  return true;
}

/**
 * 撤销设备（owner 操作）
 */
async function revokeDevice(deviceId) {
  const current = await db().collection(COLL_DEV).doc(deviceId).get().catch(() => null);
  const doc = current && current.data && current.data[0] !== undefined
    ? current.data[0] : (current && current.data) || null;
  if (!doc) return false;
  if (doc.revoked) return true;
  await db().collection(COLL_DEV).doc(deviceId).update({
    revoked: true,
    revoked_at: Math.floor(Date.now() / 1000),
  });
  return true;
}

module.exports = {
  createPairingCode,
  registerDevice,
  verifyDeviceCredential,
  bindOwner,
  revokeDevice,
  _setDbForTest: (d) => { _db = d; },
  _COLL_DEV: COLL_DEV,
  _COLL_CODE: COLL_CODE,
};
