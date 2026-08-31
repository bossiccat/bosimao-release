// A5 线上并发重放实测：同一配对码 Promise.all 双发并发注册，恰好 1 成功
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
  // 1. owner 签一个新配对码
  const r = await call('POST', '/devices/pairing-code', { bearer: OWNER, body: { platform: 'android' } });
  if (r.json.code !== 0) { console.log('FAIL: pairing-code issue failed', JSON.stringify(r.json)); process.exit(1); }
  const code = r.json.data.pairing_code;
  console.log('pairing_code =', code);

  // 2. 并发双发注册（同一个码）
  const shots = await Promise.all([
    call('POST', '/devices/register', { body: { pairing_code: code, device_name: 'concurrent-A', platform: 'android' } }),
    call('POST', '/devices/register', { body: { pairing_code: code, device_name: 'concurrent-B', platform: 'android' } }),
  ]);
  const okCount = shots.filter(s => s.json && s.json.code === 0).length;
  const failDetail = shots.map(s => `${s.status}/${s.json && s.json.code}:${s.json && s.json.message}`).join(' | ');
  console.log(`concurrent results: ${failDetail}`);
  console.log(`okCount = ${okCount}`);
  if (okCount !== 1) { console.log('FAIL: 并发重放未被原子拦截（okCount !== 1）'); process.exit(1); }

  // 3. 第三发串行也应被拒
  const third = await call('POST', '/devices/register', { body: { pairing_code: code, device_name: 'concurrent-C', platform: 'android' } });
  if (third.json.code === 0) { console.log('FAIL: 第三发串行注册成功（码已被消费仍可注册）'); process.exit(1); }
  console.log('third serial attempt rejected:', third.json.code, third.json.message);

  console.log('A5 CONCURRENT REPLAY TEST PASSED');
})().catch(e => { console.error('ERROR', e); process.exit(1); });
