// TRTC 签发云函数入口（SCF Node.js）
// 兼容两种 handler 命名：index.main_handler（标准）与 index.main（部署工具默认值）
'use strict';

const { genUserSig } = require('./usersig');
const config = require('./config');
const signing = require('./signing');

const ERR = {
  DEVICE: 40001,
  METHOD: 40500,
  PATH: 40400,
  NO_INTENT: 40401,
  CONSUMED: 40402,
  CRED: 50300,
  INTERNAL: 50000,
};

const DEVICE_RE = /^[A-Za-z0-9_-]{1,64}$/;

function ok(data) {
  return { code: 0, data, message: 'ok' };
}
function err(code, message) {
  return { code, data: null, message };
}

function checkCred() {
  if (!config.sdkAppId || !config.secretKey) return err(ERR.CRED, 'TRTC 凭据未配置（TRTC_SDKAPPID/TRTC_SECRETKEY）');
  return null;
}

function checkDevice(deviceId) {
  if (typeof deviceId !== 'string' || !DEVICE_RE.test(deviceId)) {
    return err(ERR.DEVICE, 'device_id 非法（1-64 位字母数字_-）');
  }
  if (config.deviceWhitelist.length && !config.deviceWhitelist.includes(deviceId)) {
    return err(ERR.DEVICE, 'device_id 不在白名单');
  }
  return null;
}

/** 手机：POST /api/v1/voice/session {device_id} → {room_id, user_id, user_sig, sdk_app_id, scene} */
async function handleSession(body) {
  const credErr = checkCred();
  if (credErr) return credErr;
  const deviceId = String((body || {}).device_id || '');
  const devErr = checkDevice(deviceId);
  if (devErr) return devErr;

  const roomId = await signing.issue(deviceId); // 记录意图（幂等）
  const userSig = genUserSig(config.sdkAppId, config.secretKey, deviceId, config.userSigExpireS);
  return ok({
    room_id: roomId,
    user_id: deviceId, // 契约：user_id = device_id
    user_sig: userSig,
    sdk_app_id: config.sdkAppId,
    scene: 'audio_call',
  });
}

/** PC：GET /api/v1/voice/session/pending → {intents: [{device_id, room_id, ts}]} 全部未消费意图 */
async function handlePending() {
  const intents = await signing.listPending();
  return ok({ intents });
}

/** PC sidecar：POST /api/v1/voice/session/sign {device_id, user_id} → 消费意图并签 PC userSig */
async function handleSign(body) {
  const credErr = checkCred();
  if (credErr) return credErr;
  const deviceId = String((body || {}).device_id || '');
  const userId = String((body || {}).user_id || 'jax-pc-sidecar');
  const devErr = checkDevice(deviceId);
  if (devErr) return devErr;

  const intent = await signing.consume(deviceId, userId);
  if (!intent) return err(ERR.NO_INTENT, `device_id=${deviceId} 无有效会话意图，请先调用 session 接口`);
  const userSig = genUserSig(config.sdkAppId, config.secretKey, userId, config.userSigExpireS);
  return ok({ room_id: intent.room_id, user_id: userId, user_sig: userSig, sdk_app_id: config.sdkAppId, scene: 'audio_call' });
}

/** API Gateway / HTTP 访问服务事件 → 统一处理 */
async function route(event) {
  const http = event.httpMethod || (event.requestContext && event.requestContext.httpMethod) || 'GET';
  const path = (event.path || '').split('?')[0];
  const method = String(http).toUpperCase();

  if (path.endsWith('/session') && method === 'POST') {
    let body = {};
    try {
      body = JSON.parse(event.body || '{}');
    } catch (_e) { /* 非法 JSON 按空处理 */ }
    return await handleSession(body);
  }
  if (path.endsWith('/session/pending') && method === 'GET') {
    return await handlePending();
  }
  if (path.endsWith('/session/sign') && method === 'POST') {
    let body = {};
    try {
      body = JSON.parse(event.body || '{}');
    } catch (_e) { /* 非法 JSON 按空处理 */ }
    return await handleSign(body);
  }
  if (method === 'OPTIONS') return ok({ cors: true }); // CORS 预检
  return err(method === 'GET' ? ERR.PATH : ERR.METHOD, 'not found / method not allowed');
}

function wrap(payload) {
  const json = JSON.stringify(payload);
  const cors = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  };
  return {
    statusCode: payload.code === 0 ? 200 : 400,
    headers: { 'Content-Type': 'application/json', ...cors },
    body: json,
    isBase64Encoded: false,
  };
}

exports.main_handler = async (event, context) => {
  try {
    const payload = await route(event || {});
    return wrap(payload);
  } catch (e) {
    console.error('[trtc-sign] internal error:', e && e.message, e && e.stack);
    return wrap(err(ERR.INTERNAL, 'internal error: ' + (e && e.message || 'unknown')));
  }
};

// 兼容部署工具默认 handler（index.main）
exports.main = exports.main_handler;
