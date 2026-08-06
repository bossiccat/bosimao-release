// signing 逻辑测试：mock 数据库（验证 issue/listPending/consume 语义，不触网）
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');

const signing = require('../signing');

function makeMockDb() {
  const store = new Map(); // _id -> doc
  const mock = {
    _store: store,
    collection() {
      return {
        doc(id) {
          return {
            async set(doc) { store.set(id, { _id: id, ...doc }); },
            async remove() { store.delete(id); },
          };
        },
        where(q) {
          const cond = { _id: q._id, consumed: q.consumed };
          return {
            limit() {
              return {
                async get() {
                  const data = [];
                  for (const [id, doc] of store) {
                    if (cond._id !== undefined && id !== cond._id) continue;
                    if (cond.consumed !== undefined && doc.consumed !== cond.consumed) continue;
                    data.push({ ...doc });
                  }
                  return { data };
                },
                async update(patch) {
                  let updated = 0;
                  for (const [id, doc] of store) {
                    if (cond._id !== undefined && id !== cond._id) continue;
                    if (cond.consumed !== undefined && doc.consumed !== cond.consumed) continue;
                    Object.assign(doc, patch);
                    updated += 1;
                  }
                  return { stats: { updated } };
                },
              };
            },
            async get() {
              const data = [];
              for (const [id, doc] of store) {
                if (cond._id !== undefined && id !== cond._id) continue;
                if (cond.consumed !== undefined && doc.consumed !== cond.consumed) continue;
                data.push({ ...doc });
              }
              return { data };
            },
            async update(patch) {
              let updated = 0;
              for (const [id, doc] of store) {
                if (cond._id !== undefined && id !== cond._id) continue;
                if (cond.consumed !== undefined && doc.consumed !== cond.consumed) continue;
                Object.assign(doc, patch);
                updated += 1;
              }
              return { stats: { updated } };
            },
          };
        },
      };
    },
  };
  return mock;
}

test('issue 记录意图（幂等 upsert）', async () => {
  signing._setDbForTest(makeMockDb());
  const r1 = await signing.issue('dev-1');
  const r2 = await signing.issue('dev-1');
  assert.equal(r1, 'jax-dev-1');
  assert.equal(r2, 'jax-dev-1');
  const pending = await signing.listPending();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].device_id, 'dev-1');
});

test('listPending 多设备全量可见（多实例共享的关键语义）', async () => {
  const mock = makeMockDb();
  signing._setDbForTest(mock);
  for (let i = 0; i < 12; i++) await signing.issue(`dev-${i}`);
  const pending = await signing.listPending();
  assert.equal(pending.length, 12, '12 路唤醒必须全部可见');
});

test('consume 条件更新：仅首次成功，重复消费返回 null', async () => {
  signing._setDbForTest(makeMockDb());
  await signing.issue('dev-2');
  const first = await signing.consume('dev-2', 'pc-1');
  assert.equal(first.room_id, 'jax-dev-2');
  const second = await signing.consume('dev-2', 'pc-2');
  assert.equal(second, null, '重复消费必须被拒绝（防多 PC 重复进房）');
});

test('过期意图不出现在 pending 且被清理', async () => {
  const mock = makeMockDb();
  signing._setDbForTest(mock);
  const origNow = Date.now;
  Date.now = () => origNow() - 3600 * 1000; // 1 小时前 = 过期
  await signing.issue('old-dev');
  Date.now = origNow;
  await signing.issue('fresh-dev');
  const pending = await signing.listPending();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].device_id, 'fresh-dev');
});

test('房间号规则 roomOf = prefix + device_id', () => {
  assert.equal(signing.roomOf('abc'), 'jax-abc');
});
