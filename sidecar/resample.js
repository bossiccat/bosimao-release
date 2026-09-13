'use strict';

// resample.js —— 带**抗混叠低通**的降采样器（2026-09-12 音质根因修复）
//
// 为什么必须有它
// -------------
// TRTC 下行帧实测为 **48 kHz 立体声**（sidecar 日志 `sr=48000 ch=2`）。原 `audio.js`
// 的转换是「朴素抽取」：
//     const step = 48000 / 16000;              // = 3
//     out[i] = mono[Math.floor(i * step)];     // 每隔 3 个取一个，无滤波
// 抽取前不做低通 → **8–24 kHz 的内容折叠回 0–8 kHz**，变成非谐波噪声。
// 听感就是「糊、吐字不清」，而它**不改变时长、不改变基频、不减少帧数**——
// 所以「数帧数 / 量 F0 / 量能量」全都查不出来。
//
// 受害面有两处：`rtc.js`（上行送给模型 → 模型听到的是混叠语音）
// 与 `phone.js`（录制回复 wav → 用于听感判断的音频本身就是混叠的）。
//
// 设计要点
// --------
// - 窗函数 sinc 低通（Hamming），截止取 0.45×目标率（16k → 7.2 kHz，低于 Nyquist 8k）；
// - **有状态**：跨帧保留 N−1 个历史样本。无状态滤波会让每帧首尾各 ~N/2 个样本失真，
//   在 50 Hz 帧率上形成新的周期性瑕疵（等于用一个缺陷换另一个）；
// - 非整数比（如 44100→16000）在滤波后做线性插值，避免索引抖动；
// - 纯函数式数值实现，无外部依赖，可在 Node 下直接单测。

const TARGET_RATE = 16000;

/** 设计 Hamming 窗 sinc 低通。taps 必须为奇数，返回已归一化的实数抽头。 */
function designLowpass(cutoffHz, sampleRateHz, taps = 63) {
  if (taps % 2 === 0) taps += 1;
  const center = (taps - 1) / 2;
  const wc = (2 * Math.PI * cutoffHz) / sampleRateHz; // 归一化角频率
  const h = new Float64Array(taps);
  let sum = 0;
  for (let n = 0; n < taps; n++) {
    const d = n - center;
    const sinc = d === 0 ? wc / Math.PI : Math.sin(wc * d) / (Math.PI * d);
    const win = 0.54 - 0.46 * Math.cos((2 * Math.PI * n) / (taps - 1)); // Hamming
    h[n] = sinc * win;
    sum += h[n];
  }
  for (let n = 0; n < taps; n++) h[n] /= sum; // 直流增益 = 1，避免音量变化
  return h;
}

class Downsampler {
  /**
   * @param {number} inputRate 输入采样率（如 48000）
   * @param {number} targetRate 目标采样率（默认 16000）
   * @param {number} taps FIR 抽头数（奇数）
   */
  constructor(inputRate, targetRate = TARGET_RATE, taps = 63) {
    if (!(inputRate > 0)) throw new Error('inputRate 必须为正');
    this.inputRate = inputRate;
    this.targetRate = targetRate;
    this.ratio = inputRate / targetRate;
    // 截止取目标奈奎斯特的 0.9 倍（16k → 7.2k），留过渡带
    const cutoff = 0.45 * targetRate;
    this.taps = taps % 2 === 0 ? taps + 1 : taps;
    this.h = inputRate > targetRate ? designLowpass(cutoff, inputRate, this.taps) : null;
    this.reset();
  }

  reset() {
    // 跨帧历史：上一帧末尾 N−1 个输入样本
    this.hist = new Float64Array(this.taps - 1);
  }

  /** 输入 Int16Array（单声道）→ 输出 Buffer（s16 小端，targetRate） */
  process(mono) {
    if (this.h === null) {
      // 输入率 ≤ 目标率：不做降采样（也无需抗混叠），原样返回
      const b = Buffer.alloc(mono.length * 2);
      for (let i = 0; i < mono.length; i++) b.writeInt16LE(mono[i], i * 2);
      return b;
    }
    const N = this.taps;
    const pad = N - 1;
    const buf = new Float64Array(pad + mono.length);
    buf.set(this.hist, 0);
    for (let i = 0; i < mono.length; i++) buf[pad + i] = mono[i];

    const outLen = Math.floor(mono.length / this.ratio);
    const out = Buffer.alloc(Math.max(0, outLen) * 2);
    for (let i = 0; i < outLen; i++) {
      const idx = i * this.ratio;                 // 目标第 i 个点对应的输入索引
      const x = Math.floor(idx) + pad;            // 对齐到卷积输出坐标
      const frac = idx - Math.floor(idx);
      const y0 = this._convolve(buf, x);
      const y1 = x + 1 < buf.length ? this._convolve(buf, x + 1) : y0;
      const v = y0 + (y1 - y0) * frac;
      out.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(v))), i * 2);
    }
    // 更新历史（保留末尾 pad 个输入样本）
    if (mono.length >= pad) {
      for (let k = 0; k < pad; k++) this.hist[k] = mono[mono.length - pad + k];
    } else {
      // 极短帧：整体左移后追加
      const keep = Math.max(0, pad - mono.length);
      for (let k = 0; k < keep; k++) this.hist[k] = this.hist[k + mono.length];
      for (let k = 0; k < mono.length; k++) this.hist[keep + k] = mono[k];
    }
    return out;
  }

  _convolve(buf, x) {
    let acc = 0;
    const h = this.h, N = this.taps;
    for (let j = 0; j < N; j++) acc += h[j] * buf[x - j];
    return acc;
  }
}

module.exports = { Downsampler, designLowpass, TARGET_RATE };
