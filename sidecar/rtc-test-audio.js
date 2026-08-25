'use strict';

function injectTestAudio(cloud, makeAudioFrame16k, log) {
  try {
    const seconds = 2;
    const sampleRate = 16000;
    const freq = 440;
    const n = seconds * sampleRate;
    const buf = Buffer.alloc(n * 2);
    for (let i = 0; i < n; i++) {
      const v = Math.sin(2 * Math.PI * freq * i / sampleRate) * 0.4;
      buf.writeInt16LE(Math.round(v * 32767), i * 2);
    }
    const frameBytes = 640;
    const frames = [];
    for (let i = 0; i + frameBytes <= buf.length; i += frameBytes) {
      frames.push(buf.slice(i, i + frameBytes));
    }
    let sent = 0;
    const timer = setInterval(() => {
      try {
        cloud.sendCustomAudioData(makeAudioFrame16k(frames[sent]));
        sent += 1;
        if (sent >= frames.length) {
          clearInterval(timer);
          log('TEST', `测试音频注入完成：${frames.length} 帧（${seconds}s 440Hz @16k）`);
        }
      } catch (e) {
        clearInterval(timer);
        log('ERR', `测试音频注入失败: ${e.message}`);
      }
    }, 20);
    log('TEST', `开始注入测试音频（${frames.length} 帧 @16k）`);
  } catch (e) {
    log('ERR', `injectTestAudio 异常: ${e.message}`);
  }
}

module.exports = { injectTestAudio };
