// TRTC 签发云函数配置：全部从环境变量注入，禁止硬编码
'use strict';

function num(v, def) {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : def;
}

const config = {
  sdkAppId: Number(process.env.TRTC_SDKAPPID || 0),
  secretKey: process.env.TRTC_SECRETKEY || '',
  roomPrefix: process.env.TRTC_ROOM_PREFIX || 'jax-',
  userSigExpireS: Math.min(num(process.env.TRTC_USER_SIG_EXPIRE_S, 600), 600), // 契约硬约束 ≤600
  intentFreshS: num(process.env.TRTC_INTENT_FRESH_S, 600),
  deviceWhitelist: (process.env.TRTC_DEVICE_WHITELIST || '').split(',').map((s) => s.trim()).filter(Boolean),
};

module.exports = config;
