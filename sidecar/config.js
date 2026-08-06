// sidecar 配置（命令行参数 + .env 兜底；SecretKey 仅本地冒烟/联调兜底用）
// 生产路径：userSig 由签发端点（云函数 / 本地 backend）下发，sidecar 不持有 SecretKey。
const path = require('path');
const fs = require('fs');

const SIDECAR_USER_ID = 'jax-pc-sidecar';

// 极简 .env 加载（不引入 dotenv；仅本地冒烟兜底读 TRTC_SDKAPPID/TRTC_SECRETKEY）
function loadEnv() {
  const envPath = path.resolve(__dirname, '..', '.env');
  const env = {};
  try {
    const lines = fs.readFileSync(envPath, 'utf8').split(/\r?\n/);
    for (const line of lines) {
      const m = line.match(/^\s*([A-Za-z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
  } catch (e) {
    /* .env 缺失不阻塞（生产由签发端点下发 userSig） */
  }
  return env;
}

function parseArgs() {
  const q = new URLSearchParams(window.location.search);
  const args = (q.get('args') || '').split('&').filter(Boolean);
  const out = {
    device: 'sidecar-dev-1',
    role: 'sidecar',               // sidecar | phone（phone=联调用手机模拟器）
    signUrl: 'http://127.0.0.1:8000',
    bridgeUrl: 'ws://127.0.0.1:19092',
    wav: '',                        // phone 角色：要推送给 TRTC 的上行 wav（16k s16 mono）
    outWav: '',                     // phone 角色：下行回复保存路径
    holdS: 120,
  };
  for (const a of args) {
    if (a.startsWith('--device=')) out.device = a.split('=')[1];
    if (a.startsWith('--role=')) out.role = a.split('=')[1];
    if (a.startsWith('--sign-url=')) out.signUrl = a.split('=')[1];
    if (a.startsWith('--bridge-url=')) out.bridgeUrl = a.split('=')[1];
    if (a.startsWith('--wav=')) out.wav = a.split('=')[1];
    if (a.startsWith('--out-wav=')) out.outWav = a.split('=')[1];
    if (a.startsWith('--hold=')) out.holdS = Number(a.split('=')[1]);
  }
  return out;
}

const env = loadEnv();
const ARGS = parseArgs();

module.exports = {
  ARGS,
  env,
  SIDECAR_USER_ID,
  sdkAppIdFallback: Number(env.TRTC_SDKAPPID || 0),
  secretKeyFallback: env.TRTC_SECRETKEY || '',
  roomPrefix: env.TRTC_ROOM_PREFIX || 'jax-',
};
