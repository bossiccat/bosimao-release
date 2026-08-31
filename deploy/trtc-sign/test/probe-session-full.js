// 完整链路探测：真实注册一个探针设备 → 触发 session → 检查响应字段
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
  let r = await call('POST', '/devices/pairing-code', { bearer: OWNER, body: { platform: 'android', device_name_hint: 'gz-e2e-probe' } });
  const code = r.json.data.pairing_code;
  r = await call('POST', '/devices/register', { body: { pairing_code: code, device_name: 'gz-e2e-probe' } });
  const { device_id, credential_secret } = r.json.data;
  console.log('probe device:', device_id);
  r = await call('POST', '/session', { bearer: `${device_id}.${credential_secret}`, body: { device_id, entry_point: 'pair' } });
  console.log('session status:', r.status);
  console.log('session keys:', r.json && r.json.data ? Object.keys(r.json.data).join(',') : JSON.stringify(r.json));
  console.log('expires_at:', r.json && r.json.data ? r.json.data.expires_at : 'N/A');
})().catch(e => { console.error(e.message); process.exit(1); });
