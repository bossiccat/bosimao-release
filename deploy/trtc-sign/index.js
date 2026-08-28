// TRTC 签发云函数入口（SCF Node.js，v1.2 云端商业化闭环）
// 兼容两种 handler 命名：index.main_handler（标准）与 index.main（部署工具默认值）
//
// v1.2 路由全集（对齐手机 App DeviceRegistrationApi/VoiceSessionApi 与 PC sidecar rtc.js 契约）：
//   POST /api/v1/voice/devices/pairing-code  桌面端创建配对码（扫码用）
//   POST /api/v1/voice/devices/register      手机凭配对码注册（一次性，换 device_id+secret）
//   POST /api/v1/voice/session               手机发起会话（Bearer <device_id>.<secret>，强制校验）
//   GET  /api/v1/voice/session/pending       PC sidecar 轮询意图
//   POST /api/v1/voice/session/sign          PC sidecar 消费意图并签 userSig（Bearer sidecar 凭证）
'use strict';

const { genUserSig } = require('./usersig');
const config = require('./config');
const signing = require('./signing');
const devices = require('./devices');
const terminationController = require('./termination-controller');
const terminationService = require('./termination-service');

const ERR = {
  DEVICE: 40001,
  USER: 40002,
  METHOD: 40500,
  PATH: 40400,
  NO_INTENT: 40401,
  CONSUMED: 40402,
  AUTH: 40101,
  NONCE: 40102,
  CRED: 50300,
  INTERNAL: 50000,
  NOT_FOUND: 40401,
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

function expiresAtIso() {
  return new Date(Date.now() + config.userSigExpireS * 1000).toISOString();
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

/** 解析 Authorization: Bearer <device_id>.<secret> */
function parseBearer(event) {
  const auth = (event.headers && (event.headers.authorization || event.headers.Authorization)) || '';
  const m = auth.match(/^Bearer\s+(\S+)\.(\S+)$/);
  return m ? { deviceId: m[1], secret: m[2] } : null;
}

/** 解析 sidecar 静态凭证（VOICE_SIDECAR_CREDENTIAL，与 PC 后端一致） */
function parseSidecarBearer(event) {
  const auth = (event.headers && (event.headers.authorization || event.headers.Authorization)) || '';
  const m = auth.match(/^Bearer\s+(\S+)$/);
  if (!m) return null;
  const expected = process.env.VOICE_SIDECAR_CREDENTIAL || '';
  if (!expected || m[1] !== expected) return null;
  return m[1];
}

/** 桌面端：POST /devices/pairing-code {platform, device_name_hint} → 配对码（owner 凭证保护） */
async function handlePairingCode(event, body) {
  if (!parseSidecarBearer(event) && !parseOwnerBearer(event)) {
    return err(ERR.AUTH, 'owner/sidecar 凭证缺失或不合法');
  }
  const meta = await devices.createPairingCode(
    String(body.platform || 'android'),
    String(body.device_name_hint || '')
  );
  return ok(meta);
}

function parseOwnerBearer(event) {
  const auth = (event.headers && (event.headers.authorization || event.headers.Authorization)) || '';
  const m = auth.match(/^Bearer\s+(\S+)$/);
  if (!m) return null;
  const expected = process.env.VOICE_OWNER_CREDENTIAL || '';
  if (!expected || m[1] !== expected) return null;
  return m[1];
}

function parseOwnerRouteBearer(event) {
  return parseOwnerBearer(event);
}

/** Owner：POST /devices/{device_id}/bind {account_id} → 绑定设备账户 */
async function handleBindDevice(event, deviceId, body) {
  if (!parseOwnerRouteBearer(event)) return err(ERR.AUTH, 'owner 凭证缺失或不合法');
  if (!DEVICE_RE.test(deviceId)) return err(ERR.DEVICE, 'device_id 非法（1-64 位字母数字_-）');
  const accountId = String((body || {}).account_id || '').trim();
  if (!accountId || accountId.length > 128) return err(ERR.DEVICE, 'account_id 非法');
  const bound = await devices.bindOwner(deviceId, accountId);
  return bound ? ok({ device_id: deviceId, account_id: accountId }) : err(ERR.NOT_FOUND, '设备不存在、已撤销或已绑定其他账户');
}

/** Owner：POST /devices/{device_id}/revoke → 撤销设备凭证 */
async function handleRevokeDevice(event, deviceId) {
  if (!parseOwnerRouteBearer(event)) return err(ERR.AUTH, 'owner 凭证缺失或不合法');
  if (!DEVICE_RE.test(deviceId)) return err(ERR.DEVICE, 'device_id 非法（1-64 位字母数字_-）');
  const revoked = await devices.revokeDevice(deviceId);
  return revoked ? ok({ device_id: deviceId, status: 'revoked' }) : err(ERR.NOT_FOUND, '设备不存在');
}

/** 手机：POST /devices/register {pairing_code, device_name, platform} → 注册凭证（一次性配对码） */
async function handleRegister(body) {
  const pairingCode = String((body || {}).pairing_code || '').trim();
  const deviceName = String((body || {}).device_name || '').trim();
  if (pairingCode.length < 20 || pairingCode.length > 256) {
    return err(ERR.DEVICE, 'pairing_code 长度非法（20-256）');
  }
  if (deviceName.length < 1 || deviceName.length > 80) {
    return err(ERR.DEVICE, 'device_name 长度非法（1-80）');
  }
  if (String((body || {}).platform || 'android') !== 'android') {
    return err(ERR.DEVICE, 'platform 仅支持 android');
  }
  const reg = await devices.registerDevice(pairingCode, deviceName);
  if (reg.error) return err(ERR.DEVICE, '配对码无效或已使用（' + reg.error + '）');
  return ok(reg);
}

/** 手机：POST /session {device_id, entry_point} → TRTC 进房凭证（Bearer 设备凭证强制校验） */
async function handleSession(event, body) {
  const credErr = checkCred();
  if (credErr) return credErr;
  const bearer = parseBearer(event);
  if (!bearer) return err(ERR.AUTH, 'Authorization Bearer <device_id>.<secret> 缺失');
  const deviceId = String((body || {}).device_id || '');
  if (bearer.deviceId !== deviceId) return err(ERR.DEVICE, '凭证主体与 device_id 不一致');
  const devErr = checkDevice(deviceId);
  if (devErr) return devErr;

  // v1.2 强制：云端注册的设备必须凭证校验（哈希比对，secret 永不明文存储）
  const valid = await devices.verifyDeviceCredential(deviceId, bearer.secret);
  if (!valid) return err(ERR.AUTH, '设备凭证校验失败（未注册/已撤销/已过期/secret 错误）');

  const requestedSessionId = String((body || {}).session_id || '');
  const roomId = await signing.issue(deviceId, requestedSessionId); // 记录意图（幂等，session_id 可选）
  const sessionId = requestedSessionId || roomId;
  const generation = Number.isInteger(body && body.generation) && body.generation >= 0
    ? body.generation : 0;
  await terminationService.registerSession({
    session_id: sessionId,
    device_id: deviceId,
    room_id: roomId,
    generation,
  });
  const userSig = genUserSig(config.sdkAppId, config.secretKey, deviceId, config.userSigExpireS);
  return ok({
    room_id: roomId,
    session_id: sessionId, // 手机端可选；缺省回落 room_id（sidecar bridge 契约必填）
    user_id: deviceId, // 契约：user_id = device_id
    user_sig: userSig,
    sdk_app_id: config.sdkAppId,
    expires_at: expiresAtIso(),
    scene: 'trtc_full_duplex',
  });
}

/** PC：GET /session/pending → {intents: [{device_id, room_id, ts}]} 全部未消费意图（sidecar 凭证） */
async function handlePending(event) {
  if (!parseSidecarBearer(event) && !parseOwnerBearer(event)) {
    return err(ERR.AUTH, 'sidecar/owner 凭证缺失或不合法');
  }
  const intents = await signing.listPending();
  return ok({ intents });
}

/** PC sidecar：POST /session/sign {device_id, user_id} → 消费意图并签 PC userSig（sidecar 凭证） */
async function handleSign(event, body) {
  const credErr = checkCred();
  if (credErr) return credErr;
  if (!parseSidecarBearer(event) && !parseOwnerBearer(event)) {
    return err(ERR.AUTH, 'sidecar/owner 凭证缺失或不合法');
  }
  const deviceId = String((body || {}).device_id || '');
  const userId = String((body || {}).user_id || 'jax-pc-sidecar');
  const devErr = checkDevice(deviceId);
  if (devErr) return devErr;

  const intent = await signing.consume(deviceId, userId);
  if (!intent) return err(ERR.NO_INTENT, `device_id=${deviceId} 无有效会话意图，请先调用 session 接口`);
  const userSig = genUserSig(config.sdkAppId, config.secretKey, userId, config.userSigExpireS);
  return ok({ room_id: intent.room_id, session_id: intent.session_id, user_id: userId, user_sig: userSig, sdk_app_id: config.sdkAppId, expires_at: expiresAtIso(), scene: 'trtc_full_duplex' });
}

/** API Gateway / HTTP 访问服务事件 → 统一处理 */
async function route(event) {
  const http = event.httpMethod || (event.requestContext && event.requestContext.httpMethod) || 'GET';
  const path = (event.path || '').split('?')[0];
  const method = String(http).toUpperCase();

  let body = {};
  if (method === 'POST') {
    try {
      body = JSON.parse(event.body || '{}');
    } catch (_e) { /* 非法 JSON 按空处理 */ }
  }

  const terminateRoute = path.match(/\/sessions\/([^/]+)\/terminate$/);
  if (terminateRoute && method === 'POST') {
    return await terminationController.terminate(event, terminateRoute[1], body);
  }
  const terminationRoute = path.match(/\/sessions\/([^/]+)\/termination\/([^/]+)$/);
  if (terminationRoute && method === 'GET') {
    return await terminationController.status(event, terminationRoute[1], terminationRoute[2]);
  }
  const terminationAction = path.match(
    /\/sessions\/([^/]+)\/termination\/([^/]+)\/(retry|acknowledgements)$/
  );
  if (terminationAction && method === 'POST') {
    if (terminationAction[3] === 'retry') {
      return await terminationController.retry(
        event, terminationAction[1], terminationAction[2], body
      );
    }
    return await terminationController.acknowledge(
      event, terminationAction[1], terminationAction[2], body
    );
  }

  const deviceAction = path.match(/\/devices\/([^/]+)\/(bind|revoke)$/);
  if (deviceAction && method === 'POST') {
    if (deviceAction[2] === 'bind') return await handleBindDevice(event, deviceAction[1], body);
    return await handleRevokeDevice(event, deviceAction[1]);
  }
  if (path.endsWith('/devices/pairing-code') && method === 'POST') {
    return await handlePairingCode(event, body);
  }
  if (path.endsWith('/devices/register') && method === 'POST') {
    return await handleRegister(body);
  }
  if (path.endsWith('/session') && method === 'POST') {
    return await handleSession(event, body);
  }
  if (path.endsWith('/session/pending') && method === 'GET') {
    return await handlePending(event);
  }
  if (path.endsWith('/session/sign') && method === 'POST') {
    return await handleSign(event, body);
  }
  if (method === 'OPTIONS') return ok({ cors: true }); // CORS 预检
  return err(method === 'GET' ? ERR.PATH : ERR.METHOD, 'not found / method not allowed');
}

function wrap(payload) {
  const statusOverride = payload.statusCode;
  if (statusOverride !== undefined) delete payload.statusCode;
  const json = JSON.stringify(payload);
  const cors = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization, X-Request-Nonce',
  };
  const statusCode = statusOverride || (payload.code === 0 ? 200
    : payload.code === ERR.AUTH || payload.code === ERR.NONCE ? 401
    : payload.code === 40402 || payload.code === 40403 || payload.code === ERR.NOT_FOUND ? 404
    : payload.code === ERR.METHOD ? 405
    : payload.code === ERR.CRED || payload.code === ERR.INTERNAL || payload.code === 50301 ? 503
    : payload.code >= 40900 && payload.code < 41000 ? 409
    : 400);
  return {
    statusCode,
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
    return wrap(err(ERR.INTERNAL, 'internal error'));
  }
};

// 兼容部署工具默认 handler（index.main）
exports.main = exports.main_handler;
