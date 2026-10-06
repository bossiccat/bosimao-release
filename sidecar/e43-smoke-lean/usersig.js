// TRTC UserSig 生成（TLSSigAPIv2 官方算法，Node 版）
// 对齐官方 tls-sig-api-v2-node（https://github.com/tencentyun/tls-sig-api-v2-node）：
//   HMAC-SHA256 签名 → JSON.stringify（无空格）→ zlib.deflateSync → base64 → +* /- =_
// 注意：与 Python 官方实现的 json.dumps 分隔符（含空格）不同，两者均为官方接受格式，
// 各自平台按自身实现解码；TLS.sig 的 HMAC 原文（四行 key:value）两端一致。
// SecretKey 只从 .env 注入（process.env.TRTC_SECRETKEY），本文件不落密钥。
const crypto = require('crypto');
const zlib = require('zlib');

function base64urlEscape(str) {
  return str.replace(/\+/g, '*').replace(/\//g, '-').replace(/=/g, '_');
}

/**
 * 生成 TRTC UserSig
 * @param {number} sdkAppId
 * @param {string} secretKey
 * @param {string} userId
 * @param {number} expireS 有效期秒（本项目契约 ≤600）
 * @returns {string}
 */
function genUserSig(sdkAppId, secretKey, userId, expireS = 600) {
  if (!secretKey) throw new Error('secret_key 为空：TRTC_SECRETKEY 未配置');
  const currTime = Math.floor(Date.now() / 1000);
  const raw =
    'TLS.identifier:' + userId + '\n' +
    'TLS.sdkappid:' + sdkAppId + '\n' +
    'TLS.time:' + currTime + '\n' +
    'TLS.expire:' + expireS + '\n';
  const sig = crypto
    .createHmac('sha256', secretKey)
    .update(raw, 'utf8')
    .digest('base64');
  const sigDoc = {
    'TLS.ver': '2.0',
    'TLS.identifier': String(userId),
    'TLS.sdkappid': Number(sdkAppId),
    'TLS.time': Number(currTime),
    'TLS.expire': Number(expireS),
    'TLS.sig': sig,
  };
  const compressed = zlib
    .deflateSync(Buffer.from(JSON.stringify(sigDoc), 'utf8'))
    .toString('base64');
  return base64urlEscape(compressed);
}

module.exports = { genUserSig, base64urlEscape };
