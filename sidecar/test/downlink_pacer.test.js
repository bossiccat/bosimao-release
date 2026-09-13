// downlink_pacer.test.js —— 锁定「下行必须按 20ms 实时节拍注入」这一契约。
//
// 背景（2026-09-12 卡顿根因）：rtc_bridge 突发下发（实测一次 19 帧），原实现收到即
// `sendCustomAudioData`，背靠背连发被 SDK 静默吞掉 → 手机侧音频出现能量悬崖（卡顿）。
// 本测试用**注入的假定时器**确定性地验证节拍器行为，不依赖真实时间。
//
// 运行：node --test sidecar/test/
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const { DownlinkPacer } = require(path.join(__dirname, '..', 'downlink_pacer.js'));

/** 假定时器：手动推进 tick，避免依赖真实时钟。 */
function fakeTimer() {
  const held = { fn: null, ms: null, cleared: false };
  return {
    setIntervalImpl: (fn, ms) => { held.fn = fn; held.ms = ms; return 'H'; },
    clearIntervalImpl: () => { held.cleared = true; },
    held,
    fire() { if (held.fn) held.fn(); },
  };
}

test('push 只入队、不立刻发送（禁止突发注入）', () => {
  const sent = [];
  const p = new DownlinkPacer({ send: (b) => sent.push(b) });
  for (let i = 0; i < 19; i += 1) p.push(Buffer.alloc(640, i));
  assert.strictEqual(sent.length, 0, '入队阶段不允许有任何一次 send');
  assert.strictEqual(p.pending, 19);
});

test('每个 tick 只出队一帧，且按 frameMs 周期调度', () => {
  const t = fakeTimer();
  const sent = [];
  const p = new DownlinkPacer({
    send: (b) => sent.push(b), setIntervalImpl: t.setIntervalImpl,
    clearIntervalImpl: t.clearIntervalImpl,
  });
  p.start();
  assert.strictEqual(t.held.ms, 20, '周期必须是 20ms（与链路帧长一致）');
  for (let i = 0; i < 19; i += 1) p.push(Buffer.alloc(2, i));
  t.fire(); assert.strictEqual(sent.length, 1, '一个 tick 只能送一帧');
  t.fire(); assert.strictEqual(sent.length, 2);
  assert.strictEqual(p.stats.sent, 2);
});

test('19 帧突发被摊平为 19 个 tick（这才是修复的实质）', () => {
  const t = fakeTimer();
  const sent = [];
  const p = new DownlinkPacer({
    send: (b) => sent.push(b), setIntervalImpl: t.setIntervalImpl,
    clearIntervalImpl: t.clearIntervalImpl,
  });
  p.start();
  for (let i = 0; i < 19; i += 1) p.push(Buffer.alloc(2, i));
  for (let i = 0; i < 19; i += 1) t.fire();
  assert.strictEqual(sent.length, 19);
  assert.strictEqual(p.pending, 0);
});

test('队列有界：溢出丢最旧，且计数可观测', () => {
  const t = fakeTimer();
  const drops = [];
  const p = new DownlinkPacer({
    send: () => {}, maxFrames: 3, onDrop: (i) => drops.push(i.dropped),
    setIntervalImpl: t.setIntervalImpl, clearIntervalImpl: t.clearIntervalImpl,
  });
  for (let i = 0; i < 6; i += 1) p.push(Buffer.from([i]));
  assert.strictEqual(p.pending, 3, '队列不得超过上限');
  assert.strictEqual(p.stats.dropped, 3, '丢弃数必须被计数（旧实现是静默丢）');
  assert.deepStrictEqual(drops, [1, 2, 3]);
  // maxQueue 记录**丢弃前**观察到的峰值（第 4 次 push 时队列短暂到 4）。
  // 保留这个语义是有意的：它直接量出「突发有多大」，是判断上游是否仍在突发下发的依据。
  assert.strictEqual(p.stats.maxQueue, 4);
});

test('空队列 tick 不调用 send，也不虚增 sent', () => {
  const sent = [];
  const p = new DownlinkPacer({ send: (b) => sent.push(b) });
  assert.strictEqual(p.tick(), false);
  assert.strictEqual(sent.length, 0);
  assert.strictEqual(p.stats.sent, 0);
});

// ── 欠载（underrun）可观测性 ─────────────────────────────────────────────────
// 空队列 tick = 节拍器跑满 20ms 但无帧可发：上游下行供不上（限速/被打断/上游空窗），
// 手机侧就是空洞。此前欠载**从不被记录**，是「下行有洞但归因不了」的盲区。
test('空队列 tick 计入 underruns，且不虚增 sent', () => {
  const t = fakeTimer();
  const sent = [];
  const p = new DownlinkPacer({
    send: (b) => sent.push(b), setIntervalImpl: t.setIntervalImpl,
    clearIntervalImpl: t.clearIntervalImpl,
  });
  p.start();
  for (let i = 0; i < 5; i += 1) t.fire(); // 队列恒空 → 5 次欠载
  assert.strictEqual(p.stats.underruns, 5, '每个空 tick 必须计一次欠载');
  assert.strictEqual(p.stats.ticks, 5);
  assert.strictEqual(p.stats.sent, 0, '欠载不得虚增 sent');
  assert.strictEqual(sent.length, 0);

  p.push(Buffer.alloc(2));
  t.fire();                                // 有帧 → 正常发送，欠载不再增长
  assert.strictEqual(p.stats.underruns, 5);
  assert.strictEqual(p.stats.sent, 1);
});

test('stop 后计时器必须被清除', () => {
  const t = fakeTimer();
  const p = new DownlinkPacer({
    send: () => {}, setIntervalImpl: t.setIntervalImpl, clearIntervalImpl: t.clearIntervalImpl,
  });
  p.start();
  p.stop();
  assert.strictEqual(t.held.cleared, true);
  assert.strictEqual(p.pending, 0);
});

// ── 默认定时器路径（此前的测试盲区）──────────────────────────────────────────
// 上面所有用例都注入了假定时器，因此**从未走过 `opts.setIntervalImpl` 缺省这条真实路径**。
// 结果：2026-09-12 接线后真机抛 `TypeError: Illegal invocation`
// （把 setInterval 存进属性再 `this._x()` 调用，浏览器里 this 不是 window）。
// 这条用例用**真实 setInterval** 跑一次默认路径，专门守住该类错误。
test('未注入定时器时必须走真实默认路径且不抛（守住 Illegal invocation）', async () => {
  const sent = [];
  const p = new DownlinkPacer({ send: (b) => sent.push(b), frameMs: 5 });
  assert.doesNotThrow(() => p.start(), 'start() 不得因原生定时器接收者丢失而抛');
  p.push(Buffer.alloc(2));
  p.push(Buffer.alloc(2));
  await new Promise((r) => setTimeout(r, 40));
  p.stop();
  assert.deepStrictEqual(sent.length >= 1, true, '默认路径必须真的按节拍送出帧');
});

// 欠载计数同样必须在**真实默认定时器**路径下可观测（见上「默认定时器路径」盲区教训）。
test('未注入定时器时欠载计数走真实默认路径', async () => {
  const p = new DownlinkPacer({ send: () => {}, frameMs: 5 });
  p.start();                               // 队列恒空 → 每个 tick 都是欠载
  await new Promise((r) => setTimeout(r, 40));
  p.stop();
  assert.strictEqual(p.stats.sent, 0, '空队列不得有发送');
  assert.ok(p.stats.underruns >= 1,
    `默认路径下欠载必须被计数，实得 ${p.stats.underruns}（0 说明计数没走默认路径）`);
  assert.strictEqual(p.stats.ticks, p.stats.underruns, '空队列时每个 tick 都是欠载');
});

// ── 打断冲刷：clear() ────────────────────────────────────────────────────────
// 节拍器为消除「突发注入被 SDK 吞帧」而引入队列，代价是最多积压 1 秒待播音频。
// 用户插话时必须立即清空，否则被打断的旧回复会继续播完这 1 秒（实测打断延迟
// 从 ~0.7s 涨到 1.75s，其中约 1s 由此而来）。
test('clear() 立即丢弃全部待发帧，并计入 dropped（但不动 sent）', () => {
  const t = fakeTimer();
  const sent = [];
  const p = new DownlinkPacer({
    send: (b) => sent.push(b), setIntervalImpl: t.setIntervalImpl,
    clearIntervalImpl: t.clearIntervalImpl,
  });
  p.start();                                 // 必须先注册假定时器，否则 fire() 空转
  for (let i = 0; i < 30; i += 1) p.push(Buffer.alloc(2, i));
  t.fire(); t.fire();                       // 已送 2 帧
  assert.strictEqual(p.stats.sent, 2);

  const droppedNow = p.clear();
  assert.strictEqual(droppedNow, 28, 'clear 应返回被丢弃的帧数');
  assert.strictEqual(p.pending, 0, '队列必须立即为空');
  assert.strictEqual(p.stats.dropped, 28, '丢弃必须可观测');
  assert.strictEqual(p.stats.sent, 2, 'clear 不得重置累计发送数（对账用）');

  t.fire();
  assert.strictEqual(sent.length, 2, '清空后不得再送出任何被丢弃的帧');
});

test('clear() 对空队列是安全的，且不虚增 dropped', () => {
  const p = new DownlinkPacer({ send: () => {} });
  assert.strictEqual(p.clear(), 0);
  assert.strictEqual(p.stats.dropped, 0);
});
