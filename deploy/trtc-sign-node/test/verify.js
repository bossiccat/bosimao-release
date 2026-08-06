// QA 独立验签器（不 import genUserSig 自证）：官方 TLSSigAPIv2 反解
// userSig → 自定义 base64 解码 → zlib.inflate → JSON 字段 → 按官方顺序重建签名原文 → HMAC 校验
'use strict';

const zlib = require('zlib');
const crypto = require('crypto');

// 官方 base64 自定义字母表：+→*、/→-、=→_（与 usersig.js base64urlEscape 对称）
function b64Decode(s) {
  const std = s.replace(/\*/g, '+').replace(/-/g, '/').replace(/_/g, '=');
  return Buffer.from(std, 'base64');
}

function parseUserSig(userSig, sdkAppId, secretKey) {
  const raw = b64Decode(userSig);
  const inflated = zlib.inflateSync(raw);
  const fields = JSON.parse(inflated.toString('utf8'));
  // 官方签名原文顺序固定：identifier → sdkappid → time → expire（与 genUserSig 一致）
  const rawStr =
    'TLS.identifier:' + fields['TLS.identifier'] + '\n' +
    'TLS.sdkappid:' + fields['TLS.sdkappid'] + '\n' +
    'TLS.time:' + fields['TLS.time'] + '\n' +
    'TLS.expire:' + fields['TLS.expire'] + '\n';
  const expect = crypto.createHmac('sha256', secretKey).update(rawStr, 'utf8').digest('base64');
  return {
    fields,
    sigValid: expect === fields['TLS.sig'],
    appIdMatch: Number(fields['TLS.sdkappid']) === Number(sdkAppId),
    expireOk: Number(fields['TLS.expire']) > 0 && Number(fields['TLS.expire']) <= 600,
    identifier: String(fields['TLS.identifier'] || ''),
    ver: fields['TLS.ver'],
  };
}

module.exports = { parseUserSig, b64Decode };
