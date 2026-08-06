// TRTC Electron SDK 冒烟环境渲染进程脚本（R1 gate：PCM 帧确认 + 哑对端进房）
//
// 用途：
//   1) 冒烟确认 trtc-electron-sdk 能否拿到远端音频原始 PCM 帧（R1 gate）
//   2) 哑对端：进房 jax-<device_id>，输出"进房成功 / 远端加入 / 远端离开"日志
//
// 运行：node_modules/electron/dist/electron.exe . --device=<device_id> [--user=<uid>] [--hold=<s>]
// 说明：SecretKey 只从项目根 .env 读取（TRTC_SECRETKEY），本文件不落密钥。
const path = require('path');
const fs = require('fs');
const TRTCCloud = require('trtc-electron-sdk').default;
const { genUserSig } = require('./usersig');

// ---------- 极简 .env 加载（不引入 dotenv 依赖；SecretKey 仅进程内使用） ----------
function loadEnv() {
  const envPath = path.resolve(__dirname, '..', '.env');
  const env = {};
  try {
    const lines = fs.readFileSync(envPath, 'utf8').split(/\r?\n/);
    for (const line of lines) {
      const m = line.match(/^\s*([A-Za-z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
  } catch (e) {
    console.error('[env] 无法读取 .env:', e.message);
  }
  return env;
}

const env = loadEnv();
const SDK_APP_ID = Number(env.TRTC_SDKAPPID || 0);
const SECRET_KEY = env.TRTC_SECRETKEY || '';
const ROOM_PREFIX = env.TRTC_ROOM_PREFIX || 'jax-';
const SIDECAR_USER_ID = 'jax-pc-sidecar'; // 与手机 userId("pc-phone") 区分
const EXPIRE_S = 600;

function parseArgs() {
  const q = new URLSearchParams(window.location.search);
  const args = (q.get('args') || '').split('&').filter(Boolean);
  let device = 'smoke-dev-1';
  let user = SIDECAR_USER_ID;
  let holdS = 30000;
  for (const a of args) {
    if (a.startsWith('--device=')) device = a.split('=')[1];
    if (a.startsWith('--user=')) user = a.split('=')[1];
    if (a.startsWith('--hold=')) holdS = Number(a.split('=')[1]) * 1000;
  }
  return { device, user, holdS };
}

const ARGS = parseArgs();
const DEVICE_ID = ARGS.device;
const ROOM_ID = ROOM_PREFIX + DEVICE_ID;
const RUN_USER_ID = ARGS.user;

// 日志双写：console + 本地文件（无头环境渲染进程 stdout 不可靠；按 userId 分文件避免并发覆盖）
const LOG_FILE = path.resolve(__dirname, 'logs', `smoke-${RUN_USER_ID}.log`);
try {
  fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
  fs.writeFileSync(LOG_FILE, '');
} catch (e) {
  /* 目录写失败不阻塞 */
}
function log(tag, msg) {
  const line = `[${new Date().toISOString()}] [${tag}] ${msg}`;
  console.log(line);
  try {
    fs.appendFileSync(LOG_FILE, line + '\n');
  } catch (e) {
    /* ignore */
  }
}

// ---------- R1 gate：音频 API 面探测 ----------
function probeAudioApis(cloud) {
  log('R1', '=== 音频 API 面探测（以已安装 SDK 13.3.801 类型定义为准） ===');
  const proto = Object.getPrototypeOf(cloud);
  const methods = [];
  let o = proto;
  while (o && o !== Object.prototype) {
    Object.getOwnPropertyNames(o).forEach((n) => methods.push(n));
    o = Object.getPrototypeOf(o);
  }
  const uniq = [...new Set(methods)].sort();
  const candidates = [
    'setAudioFrameCallback',
    'enableCustomAudioCapture',
    'sendCustomAudioData',
    'startLocalAudio',
    'stopLocalAudio',
    'getSDKVersion',
    'enableAudioVolumeEvaluation',
    'callExperimentalAPI',
  ];
  const found = {};
  for (const c of candidates) found[c] = uniq.includes(c);
  log('R1', '关键 API 探测结果:');
  for (const c of candidates) log('R1', `  ${c}: ${found[c] ? '✅ 存在' : '❌ 不存在'}`);
  return found;
}

// ---------- 哑对端 ----------
function main() {
  if (!SDK_APP_ID || !SECRET_KEY) {
    log('FATAL', 'TRTC_SDKAPPID / TRTC_SECRETKEY 未配置（检查项目根 .env）');
    return;
  }
  log('BOOT', `SDK_APP_ID=${SDK_APP_ID} ROOM=${ROOM_ID} USER=${RUN_USER_ID}`);
  const userSig = genUserSig(SDK_APP_ID, SECRET_KEY, RUN_USER_ID, EXPIRE_S);
  log('BOOT', `userSig 生成成功（userId=${RUN_USER_ID}, expire=${EXPIRE_S}s）`);

  const cloud = TRTCCloud.getTRTCShareInstance();

  try {
    const ver = cloud.getSDKVersion();
    log('BOOT', `trtc-electron-sdk getSDKVersion() = ${ver}`);
  } catch (e) {
    log('WARN', `getSDKVersion() 调用失败: ${e.message}`);
  }

  const api = probeAudioApis(cloud);

  cloud.on('onEnterRoom', (result) => {
    if (result > 0) {
      log('ROOM', `进房成功（elapsed=${result}ms）`);
    } else {
      log('ROOM', `进房失败 errCode=${result}`);
    }
  });
  cloud.on('onExitRoom', (reason) => {
    log('ROOM', `退房 reason=${reason}`);
  });
  cloud.on('onRemoteUserEnterRoom', (userId) => {
    log('PEER', `远端加入 userId=${userId}`);
  });
  cloud.on('onRemoteUserLeaveRoom', (userId, reason) => {
    log('PEER', `远端离开 userId=${userId} reason=${reason}`);
  });
  cloud.on('onError', (errCode, errMsg) => {
    log('ERR', `onError errCode=${errCode} msg=${errMsg}`);
  });
  cloud.on('onUserAudioAvailable', (userId, available) => {
    log('AUDIO', `远端音频可用 userId=${userId} available=${available}`);
  });

  // R1 结论
  const hasRawFrameApi = api.setAudioFrameCallback;
  const hasCustomCapture = api.enableCustomAudioCapture && api.sendCustomAudioData;
  if (hasRawFrameApi) {
    log('R1', '结论：setAudioFrameCallback 存在 —— 可拿远端原始 PCM 帧（onPlayAudioFrame 为混音前每路远端音频）');
    try {
      cloud.setAudioFrameCallback({
        onCapturedAudioFrame: (frame) => {
          log('PCM', `本地采集帧 len=${frame ? frame.length : 0} sr=${frame && frame.sampleRate} ch=${frame && frame.channel}`);
        },
        onPlayAudioFrame: (frame, userId) => {
          log('PCM', `远端音频帧 userId=${userId} len=${frame ? frame.length : 0} sr=${frame && frame.sampleRate} ch=${frame && frame.channel}`);
        },
        onMixedPlayAudioFrame: (frame) => {
          log('PCM', `混合播放帧 len=${frame ? frame.length : 0} sr=${frame && frame.sampleRate}`);
        },
        onLocalProcessedAudioFrame: null,
        onMixedAllAudioFrame: null,
      });
      log('R1', 'setAudioFrameCallback 注册成功（等待远端进房后应有 PCM 帧日志）');
    } catch (e) {
      log('R1', `setAudioFrameCallback 注册失败: ${e.message}`);
    }
  } else {
    log('R1', '结论：无 setAudioFrameCallback —— 原始 PCM 上行需走 Web Audio/AudioWorklet 兜底');
  }
  log('R1', `下行注入可用：enableCustomAudioCapture=${!!hasCustomCapture}（PC→手机推流路径）`);
  log('R1', '=== R1 探测完成；保持进房等待对端（hold 结束后自动退出）===');

  const { TRTCParams, TRTCAppScene } = require('trtc-electron-sdk');
  const params = new TRTCParams();
  params.sdkAppId = SDK_APP_ID;
  params.userId = RUN_USER_ID;
  params.userSig = userSig;
  params.strRoomId = ROOM_ID; // 字符串房间号（与手机端一致；intRoomId 必须为 0）
  log('ROOM', `enterRoom(roomId=${ROOM_ID}, userId=${RUN_USER_ID}, scene=audio_call)`);
  cloud.enterRoom(params, TRTCAppScene.TRTCAppSceneAudioCall);

  setTimeout(() => {
    log('ROOM', '冒烟窗口结束，exitRoom');
    cloud.exitRoom();
    setTimeout(() => window.close(), 500);
  }, ARGS.holdS);
}

main();
