// 模拟手机发会话（触发云端意图），观察 sidecar 真实轮询消费
const BASE = process.env.JAX_VOICE_BASE || 'https://jinhong-d2g55ycl591208475-1436773060.ap-shanghai.app.tcloudbase.com/api/v1/voice';
const fs = require('fs');
const env = fs.readFileSync('C:/Users/Administrator/WorkBuddy/监视app/.env', 'utf8');
const OWNER = env.match(/^VOICE_OWNER_CREDENTIAL=(.+)$/m)[1].trim();
async function call(method, p, { bearer, body } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  const res = await fetch(BASE + p, { method, headers, body: body ? JSON.stringify(body) : undefined });
  return { status: res.status, json: await res.json().catch(() => null) };
}
(async () => {
  let r = await call('POST', '/devices/pairing-code', { bearer: OWNER, body: { platform: 'android', device_name_hint: 'live-e2e' } });
  const code = r.json.data.pairing_code;
  r = await call('POST', '/devices/register', { body: { pairing_code: code, device_name: 'live-e2e-phone' } });
  const { device_id, credential_secret } = r.json.data;
  console.log(`phone registered: ${device_id}`);
  r = await call('POST', '/session', { bearer: `${device_id}.${credential_secret}`, body: { device_id, entry_point: 'pair' } });
  console.log(`intent issued, room=${r.json.data.room_id}`);
  console.log('sidecar 应在 2s 内轮询到并进房；观察 sidecar-sidecar.log');
})().catch(e => { console.error(e.message); process.exit(1); });
