// audio.js —— PCM 工具（16k s16 mono 全链路对齐；48k→16k 3:1 抽取；下行帧构造）
const { TRTCAudioFrame } = require('trtc-electron-sdk');

// 目标格式：16k 单声道 s16
const TARGET_RATE = 16000;

/**
 * TRTC 远端音频帧 → 16k 单声道 s16 Buffer
 * SDK 回调默认 48k（可 1/2 声道）；s16 数据按声道交叉存储。
 * 转换：多声道取平均 → 按采样率 3:1 抽取到 16k。
 * @param {{data: Buffer|ArrayBuffer, sampleRate: number, channel: number, length: number}} frame
 * @returns {Buffer|null}
 */
function frameToS16Mono16k(frame) {
  if (!frame || !frame.data) return null;
  const raw = Buffer.isBuffer(frame.data) ? frame.data : Buffer.from(frame.data);
  const sampleRate = frame.sampleRate || TARGET_RATE;
  const channel = frame.channel || 1;
  const n = Math.floor(raw.length / 2); // s16 样本数（跨声道）
  if (n < channel) return null;

  // 1) 多声道 → 单声道（取平均）
  const monoLen = Math.floor(n / channel);
  const mono = new Int16Array(monoLen);
  for (let i = 0; i < monoLen; i++) {
    let sum = 0;
    for (let c = 0; c < channel; c++) sum += raw.readInt16LE((i * channel + c) * 2);
    mono[i] = sum / channel;
  }

  // 2) 采样率 → 16k（线性抽取；16000 原样直通零开销）
  if (sampleRate === TARGET_RATE) {
    return Buffer.from(mono.buffer, mono.byteOffset, mono.byteLength);
  }
  const step = sampleRate / TARGET_RATE;
  const outLen = Math.floor(mono.length / step);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) out[i] = mono[Math.floor(i * step)];
  return Buffer.from(out.buffer, out.byteOffset, out.byteLength);
}

/**
 * 构造下行 TRTCAudioFrame（16k 单声道 s16，20ms 帧）
 * 官方 d.ts 确认 sendCustomAudioData 支持 sampleRate=16000、channel=1、PCM。
 */
function makeAudioFrame16k(buf) {
  const frame = new TRTCAudioFrame();
  frame.audioFormat = 1; // TRTCAudioFrameFormatPCM
  frame.data = buf;
  frame.length = buf.length;
  frame.sampleRate = TARGET_RATE;
  frame.channel = 1;
  frame.timestamp = Date.now();
  return frame;
}

/** s16 Buffer 按帧长拆分（默认 20ms @16k = 640B） */
function splitIntoFrames(buf, frameMs = 20) {
  const frameBytes = TARGET_RATE * 2 * (frameMs / 1000);
  const frames = [];
  for (let i = 0; i + frameBytes <= buf.length; i += frameBytes) {
    frames.push(buf.slice(i, i + frameBytes));
  }
  return frames;
}

/** 写 16k s16 mono WAV */
function writeWav16k(filePath, buffers) {
  const fs = require('fs');
  const path = require('path');
  const data = Buffer.concat(buffers);
  const header = Buffer.alloc(44);
  header.write('RIFF', 0);
  header.writeUInt32LE(36 + data.length, 4);
  header.write('WAVE', 8);
  header.write('fmt ', 12);
  header.writeUInt32LE(16, 16);          // fmt chunk size
  header.writeUInt16LE(1, 20);           // PCM
  header.writeUInt16LE(1, 22);           // mono
  header.writeUInt32LE(TARGET_RATE, 24); // sample rate
  header.writeUInt32LE(TARGET_RATE * 2, 28); // byte rate
  header.writeUInt16LE(2, 32);           // block align
  header.writeUInt16LE(16, 34);          // bits per sample
  header.write('data', 36);
  header.writeUInt32LE(data.length, 40);
  fs.mkdirSync(path.dirname(path.resolve(filePath)), { recursive: true });
  fs.writeFileSync(filePath, Buffer.concat([header, data]));
}

/** 读 16k s16 mono WAV → Buffer（仅支持本格式；联调用 tmp/poc_b3_ask_16k.wav） */
function readWav16k(filePath) {
  const fs = require('fs');
  const buf = fs.readFileSync(filePath);
  // RIFF/WAVE 校验 + data chunk 定位
  if (buf.toString('ascii', 0, 4) !== 'RIFF' || buf.toString('ascii', 8, 12) !== 'WAVE') {
    throw new Error(`不是 WAV 文件: ${filePath}`);
  }
  let off = 12;
  while (off + 8 <= buf.length) {
    const id = buf.toString('ascii', off, off + 4);
    const size = buf.readUInt32LE(off + 4);
    if (id === 'data') return buf.slice(off + 8, off + 8 + size);
    off += 8 + size + (size % 2); // 对齐到偶数
  }
  throw new Error(`WAV 无 data chunk: ${filePath}`);
}

module.exports = { frameToS16Mono16k, makeAudioFrame16k, splitIntoFrames, writeWav16k, readWav16k };
