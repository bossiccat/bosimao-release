// qa-phone 渲染进程（L1 测试用，运行时生成）：mock 手机 = 进房 + 注入 WAV + 收下行
// 用途：把 tmp/poc_b3_ask_16k.wav 通过 TRTC 自定义采集注入上行；onPlayAudioFrame 统计下行字节。
const path = require('path');
const fs = require('fs');
const TRTCCloud = require('trtc-electron-sdk').default;
const { genUserSig } = require('../usersig');

function loadEnv() {
  const env = {};
  try {
    const lines = fs.readFileSync(path.resolve(__dirname, '..', '..', '.env'), 'utf8').split(/\r?\n/);
    for (const line of lines) {
      const m = line.match(/^\s*([A-Za-z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
  } catch (e) { console.error('[env]', e.message); }
  return env;
}
const env = loadEnv();
const SDK_APP_ID = Number(env.TRTC_SDKAPPID || 0);
const SECRET_KEY = env.TRTC_SECRETKEY || '';
const ROOM_PREFIX = env.TRTC_ROOM_PREFIX || 'jax-';
const EXPIRE_S = 600;

function parseArgs() {
  const q = new URLSearchParams(window.location.search);
  const args = (q.get('args') || '').split('&').filter(Boolean);
  let device = 'l1-qa-01', holdS = 90000, wav = '';
  for (const a of args) {
    if (a.startsWith('--device=')) device = a.split('=')[1];
    if (a.startsWith('--hold=')) holdS = Number(a.split('=')[1]) * 1000;
    if (a.startsWith('--wav=')) wav = a.split('=')[1];
  }
  return { device, holdS, wav: decodeURIComponent(wav || '') };
}
const ARGS = parseArgs();
const ROOM_ID = ROOM_PREFIX + ARGS.device;
const LOG_FILE = path.resolve(__dirname, '..', 'logs', `qa-phone-${ARGS.device}.log`);
function log(tag, msg) {
  const line = `[${new Date().toISOString()}] [${tag}] ${msg}`;
  console.log(line);
  try { fs.appendFileSync(LOG_FILE, line + '\n'); } catch (e) {}
}

// 读取 WAV → 16k mono s16 PCM
function loadWav(p) {
  const buf = fs.readFileSync(p);
  // 44 字节头（本项目 WAV 均 16bit mono；若格式不同需解析 fmt chunk）
  return buf.subarray(44);
}

function main() {
  if (!SDK_APP_ID || !SECRET_KEY) { log('FATAL', 'TRTC 凭据缺失'); return; }
  const userSig = genUserSig(SDK_APP_ID, SECRET_KEY, ARGS.device, EXPIRE_S);
  const cloud = TRTCCloud.getTRTCShareInstance();
  const { TRTCParams, TRTCAppScene, TRTCAudioFrameFormat } = require('trtc-electron-sdk');

  let downBytes = 0;
  let downFrames = 0;

  cloud.on('onEnterRoom', (result) => {
    if (result > 0) {
      log('ROOM', `进房成功 elapsed=${result}ms`);
      // 进房后开启自定义采集 + 注入 WAV（mock 手机说话）
      try {
        cloud.enableCustomAudioCapture(true);
        const pcm = loadWav(ARGS.wav);
        log('WAV', `已读取 ${pcm.length}B PCM（${ARGS.wav}）`);
        // 每 40ms 推 40ms 帧（16k*2B*0.04s=1280B），循环注入 2 遍 + 尾部 1s 静音
        const frameBytes = 1280;
        let offset = 0, cycles = 0, silenceLeft = 25;
        const timer = setInterval(() => {
          let chunk;
          if (offset < pcm.length) {
            chunk = pcm.subarray(offset, Math.min(offset + frameBytes, pcm.length));
            offset += frameBytes;
            if (offset >= pcm.length) { cycles++; offset = 0; if (cycles >= 2) silenceLeft = 25; }
          } else if (silenceLeft > 0) {
            chunk = Buffer.alloc(frameBytes);
            silenceLeft--;
            if (silenceLeft <= 0) { clearInterval(timer); log('WAV', '注入结束'); }
          } else {
            return;
          }
          if (chunk.length < frameBytes) {
            const tmp = Buffer.alloc(frameBytes); chunk.copy(tmp); chunk = tmp;
          }
          const frame = new TRTCAudioFrame();
          frame.audioFormat = TRTCAudioFrameFormat.TRTCAudioFrameFormatPCM;
          frame.data = chunk;
          frame.length = chunk.length;
          frame.sampleRate = 16000;
          frame.channel = 1;
          frame.timestamp = Date.now() & 0xFFFFFFFF;
          cloud.sendCustomAudioData(frame);
        }, 40);
      } catch (e) { log('ERR', `注入失败: ${e.message}`); }
    } else {
      log('ROOM', `进房失败 errCode=${result}`);
    }
  });
  cloud.on('onRemoteUserEnterRoom', (userId) => log('PEER', `远端加入 userId=${userId}`));
  cloud.on('onRemoteUserLeaveRoom', (userId) => log('PEER', `远端离开 userId=${userId}`));
  cloud.on('onError', (errCode, errMsg) => log('ERR', `onError ${errCode} ${errMsg}`));
  cloud.on('onUserAudioAvailable', (userId, available) => log('AUDIO', `远端音频可用 ${userId} ${available}`));

  cloud.setAudioFrameCallback({
    onPlayAudioFrame: (frame, userId) => {
      const n = frame && frame.length ? frame.length : 0;
      downBytes += n;
      downFrames += 1;
      if (downFrames <= 3) log('DOWN', `首帧 userId=${userId} len=${n} sr=${frame && frame.sampleRate}`);
      if (downFrames % 20 === 0) log('DOWN', `累计 ${downBytes}B / ${downFrames} 帧`);
    },
    onCapturedAudioFrame: null, onMixedPlayAudioFrame: null,
    onLocalProcessedAudioFrame: null, onMixedAllAudioFrame: null,
  });

  const params = new TRTCParams();
  params.sdkAppId = SDK_APP_ID;
  params.userId = ARGS.device;
  params.userSig = userSig;
  params.strRoomId = ROOM_ID;
  log('BOOT', `enterRoom room=${ROOM_ID} user=${ARGS.device}`);
  cloud.enterRoom(params, TRTCAppScene.TRTCAppSceneAudioCall);

  setTimeout(() => {
    log('DOWN', `FINAL downBytes=${downBytes} downFrames=${downFrames}`);
    cloud.exitRoom();
    setTimeout(() => window.close(), 500);
  }, ARGS.holdS);
}

main();
