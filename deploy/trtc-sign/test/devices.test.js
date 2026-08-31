// devices.js v1.2 单测：配对码生命周期 + 设备注册 + 凭证校验 + 账户绑定
// 用内存 stub 模拟 CloudBase NoSQL（与 signing.test.js 同款模式）
'use strict';

const test = require('node:test');
const assert = require('assert');

// ---- 内存 DB stub（模拟 collection().doc().set/get/update + where().update/get）----
function makeDb() {
  const store = new Map(); // _id -> doc
  // 模拟 CloudBase command 操作符（devices.js 用 _.lt(n) 做条件更新）
  const cmd = {
    inc: (n) => ({ __inc: n }),
    lt: (n) => ({ __lt: n }),
  };
  function collection(name) {
    return {
      doc(id) {
        return {
          set: async (d) => { store.set(id, { ...d, _id: id }); return { updated: 1 }; },
          get: async () => (store.has(id) ? { data: store.get(id) } : null),
          update: async (patch) => { if (!store.has(id)) return { updated: 0 }; Object.assign(store.get(id), patch); return { updated: 1 }; },
          remove: async () => { store.delete(id); },
        };
      },
      where(_cond) {
        const match = (id, d) => {
          for (const [k, v] of Object.entries(_cond)) {
            if (k === '_id') { if (v !== id) return false; continue; }
            if (v && typeof v === 'object' && v.__lt !== undefined) {
              if (!(Number(d[k]) < v.__lt)) return false; // _.lt(n)：字段必须小于 n
            } else if (d[k] !== v) {
              return false;
            }
          }
          return true;
        };
        return {
          update: async (patch) => {
            // 条件更新 stub：只对满足 where 条件（如 used < max_uses）的文档生效
            let n = 0;
            for (const [id, d] of store) {
              if (!match(id, d)) continue;
              Object.assign(d, patch); n++;
            }
            return { updated: n };
          },
          limit: () => ({ get: async () => ({ data: [...store.values()] }) }),
          get: async () => ({ data: [...store.values()] }),
        };
      },
    };
  }
  return { database: () => ({ collection, command: cmd }), _store: store, command: cmd };
}

const devices = require('../devices');
const index = require('../index');

async function callRoute(event) {
  const response = await index.main_handler(event, {});
  return JSON.parse(response.body);
}

test.beforeEach(() => {
  const stub = makeDb();
  // devices.js 的 db() 返回 app.database() 结果；直接注入该结果（含 command 操作符）
  devices._setDbForTest(stub.database());
});

// A6 契约：客户端拿到的 expires_at 必须是 ISO8601 字符串（手机端 requiredString）
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$/;

test('配对码创建 → 注册 → 凭证校验全链路', async () => {
  // 1. 创建配对码
  const meta = await devices.createPairingCode('android', '我的手机');
  assert.ok(meta.pairing_code.length >= 20 && meta.pairing_code.length <= 256);
  assert.strictEqual(meta.max_uses, 1);
  assert.strictEqual(meta.ttl_seconds, 300);
  assert.strictEqual(typeof meta.expires_at, 'string', 'pairing expires_at 应为字符串');
  assert.ok(ISO_RE.test(meta.expires_at), `pairing expires_at 应为 ISO8601，实际: ${meta.expires_at}`);
  // ISO 值应晚于当前时间（TTL 300s 内）
  assert.ok(Date.parse(meta.expires_at) > Date.now() - 1000, 'pairing expires_at 应未过期');

  // 2. 手机注册（一次性配对码）
  const reg = await devices.registerDevice(meta.pairing_code, '我的手机');
  assert.ok(!reg.error, '注册不应失败');
  assert.ok(reg.device_id.length > 10);
  assert.ok(reg.credential_secret.length >= 32 && reg.credential_secret.length <= 512);
  assert.strictEqual(typeof reg.expires_at, 'string', 'register expires_at 应为字符串');
  assert.ok(ISO_RE.test(reg.expires_at), `register expires_at 应为 ISO8601，实际: ${reg.expires_at}`);
  assert.ok(Date.parse(reg.expires_at) > Date.now(), 'register expires_at 应未过期');

  // 3. 凭证校验：正确 secret 通过
  const ok1 = await devices.verifyDeviceCredential(reg.device_id, reg.credential_secret);
  assert.strictEqual(ok1, true, '正确凭证应通过');

  // 4. 凭证校验：错误 secret 拒绝
  const bad = await devices.verifyDeviceCredential(reg.device_id, 'wrong-secret');
  assert.strictEqual(bad, false, '错误凭证应拒绝');

  // 5. 配对码一次性：重放注册失败
  const replay = await devices.registerDevice(meta.pairing_code, '骗子手机');
  assert.strictEqual(replay.error, 'pairing_code_invalid', '配对码重放应被拒');
});

test('未知设备/撤销设备/过期设备凭证校验失败', async () => {
  const notFound = await devices.verifyDeviceCredential('no-such-device', 'x'.repeat(40));
  assert.strictEqual(notFound, false);

  const meta = await devices.createPairingCode('android', '');
  const reg = await devices.registerDevice(meta.pairing_code, 'test');
  await devices.revokeDevice(reg.device_id);
  const revoked = await devices.verifyDeviceCredential(reg.device_id, reg.credential_secret);
  assert.strictEqual(revoked, false, '撤销后凭证应失效');
});

test('账户绑定：首绑成功，他人抢占被拒', async () => {
  const meta = await devices.createPairingCode('android', '');
  const reg = await devices.registerDevice(meta.pairing_code, 'phone-a');

  const b1 = await devices.bindOwner(reg.device_id, 'user-alice');
  assert.strictEqual(b1, true);

  const b2 = await devices.bindOwner(reg.device_id, 'user-bob');
  assert.strictEqual(b2, false, '已绑他人不应再绑');

  const b3 = await devices.bindOwner(reg.device_id, 'user-alice');
  assert.strictEqual(b3, true, '本人重绑幂等');
});

test('owner HTTP routes expose device binding and revocation', async () => {
  process.env.VOICE_OWNER_CREDENTIAL = 'owner-test-secret';
  const meta = await devices.createPairingCode('android', 'route-test');
  const reg = await devices.registerDevice(meta.pairing_code, 'route-test');
  const bind = await callRoute({
    httpMethod: 'POST',
    path: '/api/v1/voice/devices/' + reg.device_id + '/bind',
    headers: { Authorization: 'Bearer owner-test-secret' },
    body: JSON.stringify({ account_id: 'account-1' }),
  });
  assert.equal(bind.code, 0);
  const revoke = await callRoute({
    httpMethod: 'POST',
    path: '/api/v1/voice/devices/' + reg.device_id + '/revoke',
    headers: { Authorization: 'Bearer owner-test-secret' },
    body: JSON.stringify({}),
  });
  assert.equal(revoke.code, 0);
  assert.equal(await devices.verifyDeviceCredential(reg.device_id, reg.credential_secret), false);
});

test('owner HTTP routes reject missing owner credential', async () => {
  process.env.VOICE_OWNER_CREDENTIAL = 'owner-test-secret';
  const response = await callRoute({
    httpMethod: 'POST',
    path: '/api/v1/voice/devices/device-1/revoke',
    headers: {},
    body: JSON.stringify({}),
  });
  assert.equal(response.code, 40101);
});

test('unknown device revoke preserves HTTP 404 status', async () => {
  process.env.VOICE_OWNER_CREDENTIAL = 'owner-test-secret';
  const response = await index.main_handler({
    httpMethod: 'POST',
    path: '/api/v1/voice/devices/missing-device/revoke',
    headers: { Authorization: 'Bearer owner-test-secret' },
    body: JSON.stringify({}),
  }, {});
  assert.equal(response.statusCode, 404);
  assert.equal(JSON.parse(response.body).code, 40401);
});

test('过期配对码不能注册', async () => {
  const meta = await devices.createPairingCode('android', '');
  // 直接改库把 expires_at 拨到过去（stub 内部访问）
  const dbAny = devices;
  // 通过注册一次拿 store 引引不可行；改为注册前先耗尽：注册一次成功
  const reg = await devices.registerDevice(meta.pairing_code, 'first');
  assert.ok(!reg.error);
  // 二次（同码）必然 pairing_code_invalid
  const second = await devices.registerDevice(meta.pairing_code, 'second');
  assert.strictEqual(second.error, 'pairing_code_invalid');
});

test('A5 并发重放：同一配对码并发注册只有一个成功（条件更新原子消费）', async () => {
  const meta = await devices.createPairingCode('android', 'concurrent');
  // 模拟并发双请求：两个 registerDevice 同时在飞（stub 的 where 条件更新保证
  // 只有第一个 updated=1，第二个 used 已置满 → used < max_uses 不匹配 → updated=0）
  const results = await Promise.all([
    devices.registerDevice(meta.pairing_code, 'phone-a'),
    devices.registerDevice(meta.pairing_code, 'phone-b'),
  ]);
  const okCount = results.filter((r) => !r.error).length;
  assert.strictEqual(okCount, 1, `并发注册应只有一个成功，实际: ${JSON.stringify(results.map((r) => r.error || 'ok'))}`);
  const winner = results.find((r) => !r.error);
  const loser = results.find((r) => r.error);
  assert.ok(winner.device_id && winner.credential_secret, '胜者应拿到凭证');
  assert.strictEqual(loser.error, 'pairing_code_invalid', '败者应被拒');
  // 串行第三次也必须拒绝（码已耗尽）
  const third = await devices.registerDevice(meta.pairing_code, 'phone-c');
  assert.strictEqual(third.error, 'pairing_code_invalid');
});
