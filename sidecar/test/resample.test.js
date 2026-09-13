// resample.test.js —— 锁定「降采样必须先抗混叠」这一契约。
//
// 背景（2026-09-12 音质根因）：TRTC 下行是 48 kHz，原实现每隔 3 个样本直接抽取、
// 不做低通 → 8–24 kHz 折叠回 0–8 kHz 变成非谐波噪声（听感「糊、吐字不清」）。
// 本测试用**可判定的正负例**守住修复：
//   · 12 kHz 正弦（在 8 kHz 以上）降采样后**必须几乎消失**；
//   · 1 kHz 正弦（在带内）**必须保留**，幅度基本不变；
//   · 对照：朴素抽取会让 12 kHz 残留出巨大的折叠分量 —— 证明这条测试确有区分力。
//
// 运行：node --test sidecar/test/
'use strict';

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const { Downsampler, designLowpass } = require(path.join(__dirname, '..', 'resample.js'));

const IN_RATE = 48000;
const OUT_RATE = 16000;

function tone(freq, seconds, rate, amp = 0.6) {
  const n = Math.round(seconds * rate);
  const s = new Int16Array(n);
  for (let i = 0; i < n; i++) {
    s[i] = Math.round(amp * 32767 * Math.sin((2 * Math.PI * freq * i) / rate));
  }
  return s;
}

/** 用 Goertzel 算法量某个频点的能量（比 FFT 轻，够用） */
function tonePower(buf, freq, rate) {
  const n = buf.length / 2;
  const k = (2 * Math.PI * freq) / rate;
  const coeff = 2 * Math.cos(k);
  let s1 = 0, s2 = 0;
  for (let i = 0; i < n; i++) {
    const v = buf.readInt16LE(i * 2) / 32768;
    const s0 = v + coeff * s1 - s2;
    s2 = s1; s1 = s0;
  }
  const real = s1 - s2 * Math.cos(k);
  const imag = s2 * Math.sin(k);
  return (real * real + imag * imag) / (n * n);
}

function rms(buf) {
  const n = buf.length / 2;
  let s = 0;
  for (let i = 0; i < n; i++) { const v = buf.readInt16LE(i * 2); s += v * v; }
  return Math.sqrt(s / Math.max(1, n));
}

/** 对照用：原实现的朴素抽取 */
function naiveDecimate(mono, ratio) {
  const outLen = Math.floor(mono.length / ratio);
  const out = Buffer.alloc(outLen * 2);
  for (let i = 0; i < outLen; i++) out.writeInt16LE(mono[Math.floor(i * ratio)], i * 2);
  return out;
}

test('低通抽头已归一化（直流增益=1，不改变音量）', () => {
  const h = designLowpass(7200, IN_RATE, 63);
  let sum = 0;
  for (const v of h) sum += v;
  assert.ok(Math.abs(sum - 1) < 1e-9, `抽头和应为 1，实际 ${sum}`);
});

test('带外 12kHz 正弦：降采样后必须几乎消失（抗混叠生效）', () => {
  const mono = tone(12000, 0.5, IN_RATE);
  const ds = new Downsampler(IN_RATE, OUT_RATE);
  const out = ds.process(mono);
  const alias = tonePower(out, Math.abs(12000 - OUT_RATE), OUT_RATE); // 12k 折叠到 4k
  const total = rms(out) / 32768;
  assert.ok(alias < 1e-6, `12kHz 折叠分量应被抑制，实测功率 ${alias}`);
  assert.ok(total < 0.02, `带外信号整体应被大幅衰减，实测 rms ${total}`);
});

test('带外 12kHz：朴素抽取会残留巨大折叠分量（证明测试有区分力）', () => {
  const mono = tone(12000, 0.5, IN_RATE);
  const bad = naiveDecimate(mono, IN_RATE / OUT_RATE);
  const alias = tonePower(bad, 4000, OUT_RATE);
  assert.ok(alias > 1e-3, `朴素抽取本应残留明显 4kHz 折叠分量，实测 ${alias}`);
});

test('带内 1kHz 正弦：必须保留，且幅度基本不变', () => {
  const mono = tone(1000, 0.5, IN_RATE);
  const ds = new Downsampler(IN_RATE, OUT_RATE);
  const out = ds.process(mono);
  const p = tonePower(out, 1000, OUT_RATE);
  const amp = Math.sqrt(p) * 2;              // 单边幅度
  assert.ok(Math.abs(amp - 0.6) < 0.12, `1kHz 幅度应≈0.6，实测 ${amp.toFixed(3)}`);
});

test('跨帧有状态：分段处理与整段处理的输出一致（避免帧边界瑕疵）', () => {
  const mono = tone(1000, 0.4, IN_RATE);
  const whole = new Downsampler(IN_RATE, OUT_RATE).process(mono);

  const chunk = Math.round(0.02 * IN_RATE); // 20ms
  const ds = new Downsampler(IN_RATE, OUT_RATE);
  const parts = [];
  for (let i = 0; i < mono.length; i += chunk) {
    parts.push(ds.process(mono.subarray(i, Math.min(mono.length, i + chunk))));
  }
  const piecewise = Buffer.concat(parts);
  const n = Math.min(whole.length, piecewise.length);
  let maxDiff = 0;
  for (let i = 0; i < n; i += 2) {
    maxDiff = Math.max(maxDiff, Math.abs(whole.readInt16LE(i) - piecewise.readInt16LE(i)));
  }
  // 允许 1 个量化级差异（末尾插值边界）
  assert.ok(maxDiff <= 2, `分段与整段的输出应几乎一致，最大差 ${maxDiff}`);
});

test('输入率 ≤ 目标率时原样直通（不做无意义的滤波）', () => {
  const mono = tone(3000, 0.05, OUT_RATE);
  const out = new Downsampler(OUT_RATE, OUT_RATE).process(mono);
  assert.strictEqual(out.length, mono.length * 2);
  for (let i = 0; i < mono.length; i++) {
    assert.strictEqual(out.readInt16LE(i * 2), mono[i]);
  }
});
