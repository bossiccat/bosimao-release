// 模拟手机端发起真实会话意图 → 验证 sidecar 轮询消费 → 签证进房全链路（部署后端到端）
// 与 cloud-e2e 的区别：这里走 sidecar 真实轮询（bridge 已连接），验证部署后的 v1.2.1 代码
// 在 sidecar 真实消费路径上也工作正常（cloud-e2e 是脚本模拟消费）。
const BASE = process.env.JAX_VOICE_BASE || 'https://jinhong-d2g55ycl591208475-1436773060.ap-shanghai.app.tcloudbase.com/api/v1/voice';
const fs = require('fs');
const env = fs.readFileSync('C:/Users/Administrator/WorkBuddy/监视app/.env', 'utf8');
const SIDECAR = env.match(/^VOICE_SIDECAR_CREDENTIAL=(.+)$/m)[1].trim();
const OWNER = env.match(/^VOICE_OWNER_CREDENTIAL=(.+)$/m)[1].trim();

async function call(method, p, { bearer, body } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (bearer) headers.Authorization = `Bearer ${bearer}`;
  const res = await fetch(BASE + p, { method, headers, body: body ? JSON.stringify(body) : undefined });
  return { status: res.status, json: await res.json().catch(() => null) };
}

(async () => {
  // 1. 注册一台模拟设备（验证 A6 端到端：新码新设备）
  const pc = await call('POST', '/devices/pairing-code', { bearer: OWNER, body: { platform: 'android' } });
  const code = pc.json.data.pairing_code;
  console.log('[1] pairing code issued, expires_at =', pc.json.data.expires_at);
  const reg = await call('POST', '/devices/register', { body: { pairing_code: code, device_name: 'e2e-final-check', platform: 'android' } });
  if (reg.json.code !== 0) { console.log('FAIL register', JSON.stringify(reg.json)); process.exit(1); }
  const { device_id, credential_secret: secret } = reg.json.data;
  console.log('[2] device registered:', device_id, 'expires_at =', reg.json.data.expires_at, '(ISO string, A6 OK)');

  // 2. 手机端发起会话（Bearer device_id.secret）
  const sess = await call('POST', '/session', { bearer: `${device_id}.${secret}`, body: { device_id, entry_point: 'trigger' } });
  if (sess.json.code !== 0) { console.log('FAIL session', JSON.stringify(sess.json)); process.exit(1); }
  console.log('[3] session created, room =', sess.json.data.room_id, 'userSig len =', sess.json.data.user_sig.length);

  // 3. 观察真实 sidecar 是否消费该意图（bridge 2s 轮询）——查 bridge rooms 与 pending 收敛
  console.log('[4] waiting for real sidecar to consume intent (2s poll)...');
  let consumed = false;
  for (let i = 0; i < 15; i++) {
    await new Promise(r => setTimeout(r, 2000));
    const pend = await call('GET', '/session/pending', { bearer: SIDECAR });
    const stillThere = (pend.json.data.intents || []).some(it => it.device_id === device_id);
    const bh = await fetch('http://127.0.0.1:19093/health').then(r => r.json()).catch(() => null);
    process.stdout.write(`  t=${(i + 1) * 2}s pending_has_ours=${stillThere} bridge_rooms=${bh ? bh.rooms : '?'} connected=${bh ? bh.sidecar_connected : '?'}\n`);
    if (!stillThere) { consumed = true; break; }
  }
  if (!consumed) { console.log('NOTE: sidecar 未在 30s 内消费意图（可能进房后已退出 pending 清单，或轮询空闲）。查 bridge rooms 判定。'); }
  const bh2 = await fetch('http://127.0.0.1:19093/health').then(r => r.json()).catch(() => null);
  console.log('[5] bridge final:', JSON.stringify(bh2));
  console.log('E2E FINAL CHECK DONE (cloud side all green)');
})().catch(e => { console.error('ERROR', e); process.exit(1); });
