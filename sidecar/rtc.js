// rtc.js —— sidecar 渲染进程主逻辑
//
// 角色：
//   --role=sidecar（默认）：无头对端。拉 userSig（优先签发端点 127.0.0.1:8000，
//                           失败回退 .env 本地签发）→ 进房 jax-<device_id>（userId=jax-pc-sidecar）
//                           → setAudioFrameCallback 收手机远端 PCM（48k→16k 3:1 抽取）
//                           → localhost WS 推 rtc_bridge；rtc_bridge 下行 16k s16 → sendCustomAudioData 回传手机。
//   --role=phone：联调用手机模拟器（进同房，推 wav 上行，收回复写 wav），见 phone.js。
//
// 安全：生产 userSig 由签发端点下发，sidecar 不持有 SecretKey；.env 兜底仅限本地冒烟/联调。
const config = require('./config');
const makeLogger = require('./logger');
const { genUserSig } = require('./usersig');
const { BridgeClient } = require('./bridge');
const { frameToS16Mono16k, makeAudioFrame16k } = require('./audio');
const TRTCCloud = require('trtc-electron-sdk').default;
const { TRTCParams, TRTCAppScene } = require('trtc-electron-sdk');

const { ARGS } = config;
const log = makeLogger('sidecar', `sidecar-${ARGS.role}.log`);
const cloud = TRTCCloud.getTRTCShareInstance();

const stats = { upFrames: 0, upBytes: 0, downFrames: 0, downBytes: 0 };
let bridge = null;
let exited = false;

// ---------- 签发（优先签发端点，失败回退 .env 本地签发） ----------
async function fetchSig(role) {
  const endpoint = role === 'sidecar' ? `${ARGS.signUrl}/api/v1/voice/session/sign`
                                      : `${ARGS.signUrl}/api/v1/voice/session`;
  const body = role === 'sidecar'
    ? { device_id: ARGS.device, user_id: config.SIDECAR_USER_ID }
    : { device_id: ARGS.device };
  try {
    const resp = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const parsed = await resp.json();
    if (parsed.code === 0 && parsed.data && parsed.data.user_sig) {
      log('SIG', `签发端点下发凭证 room=${parsed.data.room_id} user=${parsed.data.user_id}`);
      return parsed.data;
    }
    throw new Error(`签发失败: ${JSON.stringify(parsed)}`);
  } catch (e) {
    if (!config.secretKeyFallback) throw new Error(`签发端点不可用且无 .env 兜底: ${e.message}`);
    log('SIG', `签发端点不可用（${e.message}），回退 .env 本地签发（仅限冒烟/联调）`);
    const userId = role === 'sidecar' ? config.SIDECAR_USER_ID : ARGS.device;
    return {
      room_id: config.roomPrefix + ARGS.device,
      user_id: userId,
      user_sig: genUserSig(config.sdkAppIdFallback, config.secretKeyFallback, userId, 600),
      sdk_app_id: config.sdkAppIdFallback,
      scene: 'audio_call',
    };
  }
}

// ---------- TRTC 进房 ----------
function enterRoom(cred) {
  const params = new TRTCParams();
  params.sdkAppId = Number(cred.sdk_app_id);
  params.userId = cred.user_id;
  params.userSig = cred.user_sig;
  params.strRoomId = cred.room_id; // 字符串房间号（与手机端一致；intRoomId 必须为 0）
  log('ROOM', `enterRoom(roomId=${cred.room_id}, userId=${cred.user_id}, scene=audio_call)`);
  cloud.enterRoom(params, TRTCAppScene.TRTCAppSceneAudioCall);
}

// ---------- sidecar 主流程 ----------
function runSidecar(cred) {
  // 下行注入：先停本地麦克风再开启自定义采集（官方 d.ts 要求互斥）
  try { cloud.stopLocalAudio(); } catch (e) { /* ignore */ }
  try { cloud.enableCustomAudioCapture(true); } catch (e) { log('ERR', `enableCustomAudioCapture 失败: ${e.message}`); }

  bridge = new BridgeClient(
    ARGS.bridgeUrl,
    (buf) => { // 下行：rtc_bridge 推来的 16k s16 → sendCustomAudioData 回传手机
      try {
        cloud.sendCustomAudioData(makeAudioFrame16k(buf));
        stats.downFrames += 1;
        stats.downBytes += buf.length;
      } catch (e) {
        log('ERR', `sendCustomAudioData 失败: ${e.message}`);
      }
    },
    (action, reason) => { // 控制面
      log('CTRL', `收到 ctrl action=${action} reason=${reason}`);
      if (action === 'exit' && !exited) exitSidecar(reason || 'ctrl_exit');
    },
  );
  bridge.start({
    type: 'hello', role: 'sidecar', sdk_version: getSdkVersion(),
    device_id: ARGS.device, room_id: cred.room_id, user_id: cred.user_id,
  });

  // 远端音频回调 → 16k s16 → WS 上行
  let firstFrameLogged = false;
  cloud.setAudioFrameCallback({
    onPlayAudioFrame: (frame, userId) => {
      if (!frame || !frame.data) return;
      if (!firstFrameLogged) {
        log('PCM', `首帧: userId=${userId} sampleRate=${frame.sampleRate} channel=${frame.channel} length=${frame.length}`);
        firstFrameLogged = true;
      }
      const pcm = frameToS16Mono16k(frame);
      if (pcm && pcm.length > 0) {
        bridge.sendUpAudio(pcm);
        stats.upFrames += 1;
        stats.upBytes += pcm.length;
      }
    },
    onCapturedAudioFrame: null,
    onLocalProcessedAudioFrame: null,
    onMixedPlayAudioFrame: null,
    onMixedAllAudioFrame: null,
  });

  cloud.on('onEnterRoom', (result) => {
    if (result > 0) log('ROOM', `进房成功（elapsed=${result}ms）`);
    else log('ROOM', `进房失败 errCode=${result}`);
  });
  cloud.on('onExitRoom', (reason) => log('ROOM', `退房 reason=${reason}`));
  cloud.on('onRemoteUserEnterRoom', (userId) => {
    log('PEER', `远端加入 userId=${userId}`);
    if (bridge) bridge.sendPeerState('enter', userId);
  });
  cloud.on('onRemoteUserLeaveRoom', (userId, reason) => {
    log('PEER', `远端离开 userId=${userId} reason=${reason}`);
    if (bridge) bridge.sendPeerState('leave', userId);
    // 对端离开 → 退房回待命（PC-INTEGRATION §5.2）
    if (ARGS.role === 'sidecar' && !exited) {
      log('ROOM', '对端已离开，退房回待命');
      setTimeout(() => exitSidecar('peer_left'), 500);
    }
  });
  cloud.on('onError', (errCode, errMsg) => log('ERR', `onError errCode=${errCode} msg=${errMsg}`));
  cloud.on('onUserAudioAvailable', (userId, available) => {
    log('AUDIO', `远端音频可用 userId=${userId} available=${available}`);
  });
  cloud.on('onUserSigExpired', () => {
    log('SIG', 'userSig 过期回调；由 rtc_bridge 侧重新签发后重进房（MVP 记录日志）');
  });

  enterRoom(cred);

  // 周期统计
  setInterval(() => {
    log('STAT', `up=${stats.upFrames}帧/${(stats.upBytes / 1024).toFixed(0)}KB down=${stats.downFrames}帧/${(stats.downBytes / 1024).toFixed(0)}KB ws=${bridge ? bridge.connected : false}`);
  }, 5000);

  // hold 超时退出（默认 120s）
  setTimeout(() => { if (!exited) exitSidecar('hold_timeout'); }, ARGS.holdS * 1000);
}

function exitSidecar(reason) {
  if (exited) return;
  exited = true;
  log('ROOM', `退出 sidecar（reason=${reason}）`);
  try { cloud.exitRoom(); } catch (e) { /* ignore */ }
  if (bridge) bridge.close();
  setTimeout(() => window.close(), 400);
}

function getSdkVersion() {
  try { return cloud.getSDKVersion(); } catch (e) { return 'unknown'; }
}

// ---------- 入口 ----------
async function main() {
  log('BOOT', `role=${ARGS.role} device=${ARGS.device} signUrl=${ARGS.signUrl} bridgeUrl=${ARGS.bridgeUrl}`);
  if (!ARGS.device) { log('FATAL', '缺少 --device=<device_id>'); return; }
  try {
    const ver = getSdkVersion();
    log('BOOT', `trtc-electron-sdk getSDKVersion() = ${ver}`);
  } catch (e) { /* ignore */ }

  if (ARGS.role === 'phone') {
    require('./phone').runPhone(cloud, log);
    return;
  }

  try {
    const cred = await fetchSig('sidecar');
    runSidecar(cred);
  } catch (e) {
    log('FATAL', `启动失败: ${e.message}`);
  }
}

main();
