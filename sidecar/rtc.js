// rtc.js —— sidecar 渲染进程主逻辑
//
// 角色：
//   --role=sidecar（默认）：无头对端。使用受保护宿主运行时注入的控制面凭证拉取 userSig；
//                           凭证缺失或签发失败时 fail-closed，不在本地签名。
//                           进房 jax-<device_id>（userId=jax-pc-sidecar）
//                           → setAudioFrameCallback 收手机远端 PCM（48k→16k 3:1 抽取）
//                           → localhost WS 推 rtc_bridge；rtc_bridge 下行 16k s16 → sendCustomAudioData 回传手机。
//   --role=phone：联调用手机模拟器（进同房，推 wav 上行，收回复写 wav），见 phone.js。
//
// 安全：生产仅接受 VOICE_SIDECAR_CREDENTIAL 运行时注入；sidecar 不持有或派生 TRTC SecretKey。
// Tauri OS-bound credential 注入尚未完成，是商业发布 P0 阻断项。
const config = require('./config');
const makeLogger = require('./logger');
const { BridgeClient } = require('./bridge');
const { controlPlaneHeaders } = require('./security');
const { requestRendererExit } = require('./exit-protocol');
const { startPollingRuntime } = require('./rtc-startup');
const { frameToS16Mono16k, makeAudioFrame16k } = require('./audio');
const TRTCCloud = require('trtc-electron-sdk').default;
const { TRTCParams, TRTCAppScene } = require('trtc-electron-sdk');

const { ARGS } = config;
const log = makeLogger('sidecar', `sidecar-${ARGS.role}.log`);
const cloud = TRTCCloud.getTRTCShareInstance();

const stats = { upFrames: 0, upBytes: 0, downFrames: 0, downBytes: 0 };
let bridge = null;
let exited = false;

// ---------- 签发：控制面失败时关闭失败，不在 sidecar 本地签名 ----------

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
function runSidecar() {
  // 下行注入：先停本地麦克风再开启自定义采集（官方 d.ts 要求互斥）
  try { cloud.stopLocalAudio(); } catch (e) { /* ignore */ }
  try { cloud.enableCustomAudioCapture(true); } catch (e) { log('ERR', `enableCustomAudioCapture 失败: ${e.message}`); }

  bridge = new BridgeClient(
    ARGS.bridgeUrl,
    (buf) => { // 下行：rtc_bridge 推来的 16k s16（完整 640B 帧）→ 直接注入（Task 9：实际 SDK 契约支持 16k）
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
      // E2E 测试（v0.6.4）：注入 2s 440Hz 测试音频上行 → TRTC 分发给手机端，
      // 用于验证「AI 音频 → 手机端播放」链路（手机端 DiagLog 应出现 firstAudioFrame/voiceVolume）
      if (action === 'test_audio') injectTestAudio();
    },
  );
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
    // v0.6.3 修复：原实现 setTimeout(exitSidecar('peer_left')) → window.close() 杀死整个进程，
    // 用户下次进房无人接听（手机侧显示已连接但 AI 不回复）。正确行为 = 退房 + 清房间 +
    // 保持轮询待命（pollAndJoin 继续每 2s 检查新意图），进程常驻。
    if (ARGS.role === 'sidecar' && !exited) {
      log('ROOM', '对端已离开，退房回待命（保持轮询）');
      currentRoom = null;
      if (bridge) bridge.clearSession();
      try { cloud.exitRoom(); } catch (e) { /* ignore */ }
    }
  });
  cloud.on('onError', (errCode, errMsg) => log('ERR', `onError errCode=${errCode} msg=${errMsg}`));
  cloud.on('onUserAudioAvailable', (userId, available) => {
    log('AUDIO', `远端音频可用 userId=${userId} available=${available}`);
  });
  cloud.on('onUserSigExpired', () => {
    log('SIG', 'userSig 过期回调；由 rtc_bridge 侧重新签发后重进房（MVP 记录日志）');
  });

  // v0.6.1：进房由意图轮询 pollAndJoin 触发（不再启动即进房）

  // 周期统计
  setInterval(() => {
    log('STAT', `up=${stats.upFrames}帧/${(stats.upBytes / 1024).toFixed(0)}KB down=${stats.downFrames}帧/${(stats.downBytes / 1024).toFixed(0)}KB ws=${bridge ? bridge.connected : false}`);
  }, 5000);

}


// ---------- 意图轮询（v0.6.1）：PC 不知道手机 device_id，枚举 pending 进对应房间 ----------
let currentRoom = null;
let pollingBusy = false;

async function fetchSigForDevice(deviceId) {
  // sign_for_sidecar 会消费意图（防重复进房），返回同一房间的 PC userSig
  const resp = await fetch(`${ARGS.signUrl}/api/v1/voice/session/sign`, {
    method: 'POST',
    headers: controlPlaneHeaders({ credential: config.sidecarCredential }),
    body: JSON.stringify({ device_id: deviceId, user_id: config.SIDECAR_USER_ID }),
  });
  const parsed = await resp.json();
  if (parsed.code === 0 && parsed.data && parsed.data.user_sig) {
    log('SIG', '意图消费成功');
    return parsed.data;
  }
  throw new Error(`sign failed code=${Number(parsed.code) || 50300}`);
}

async function pollAndJoin() {
  if (exited || pollingBusy) return;
  pollingBusy = true;
  try {
    const resp = await fetch(`${ARGS.signUrl}/api/v1/voice/session/pending`, {
      method: 'GET',
      headers: controlPlaneHeaders({ credential: config.sidecarCredential }),
    });
    const parsed = await resp.json();
    const intents = (parsed.data && parsed.data.intents) || [];
    if (intents.length === 0) { pollingBusy = false; return; }
    const intent = intents[0];
    if (currentRoom === intent.room_id) { pollingBusy = false; return; }
    log('SIG', '发现会话意图');
    if (currentRoom) {
      try { cloud.exitRoom(); } catch (e) { /* ignore */ }
      await new Promise(r => setTimeout(r, 600));
    }
    const cred = await fetchSigForDevice(intent.device_id);
    if (cred.room_id !== intent.room_id) {
      if (bridge) bridge.clearSession();
      throw new Error('SIDECAR_SESSION_ROOM_MISMATCH');
    }
    bridge.startSession({
      session_id: intent.session_id,
      device_id: intent.device_id,
      room_id: cred.room_id,
      user_id: cred.user_id,
      sdk_version: getSdkVersion(),
    });
    currentRoom = cred.room_id;
    enterRoom(cred);
  } catch (e) {
    log('ERR', `意图轮询失败: ${e.message}`);
  }
  pollingBusy = false;
}
function exitSidecar(reason) {
  if (exited) return;
  exited = true;
  log('ROOM', `退出 sidecar（reason=${reason}）`);
  try { cloud.exitRoom(); } catch (e) { /* ignore */ }
  if (bridge) bridge.close();
  setTimeout(() => requestRendererExit('controlled'), 400);
}

function getSdkVersion() {
  try { return cloud.getSDKVersion(); } catch (e) { return 'unknown'; }
}

/**
 * E2E 测试音频注入：生成 2s 440Hz 正弦波（16k s16 mono，模型侧契约），
 * 经 sendCustomAudioData 上行 → TRTC 分发给手机端，验证下行播放链路。
 * 手机端 DiagLog 应记录 firstAudioFrame + voiceVolume > 0。
 */
function injectTestAudio() {
  try {
    const seconds = 2;
    const sampleRate = 16000; // 实际 SDK 契约支持 16000（Task 9 以 d.ts 为准）
    const freq = 440;
    const n = seconds * sampleRate;
    const buf = Buffer.alloc(n * 2);
    for (let i = 0; i < n; i++) {
      const v = Math.sin(2 * Math.PI * freq * i / sampleRate) * 0.4;
      buf.writeInt16LE(Math.round(v * 32767), i * 2);
    }
    // 16k 20ms 帧 = 640B
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

// ---------- 入口 ----------
async function main() {
  log('BOOT', `role=${ARGS.role}`);
  if (ARGS.invalid || !['sidecar', 'phone'].includes(ARGS.role)) {
    log('FATAL', 'SIDECAR_INVALID_ARGS');
    requestRendererExit('fatal');
    return;
  }
  if (ARGS.role === 'sidecar' && ARGS.device !== undefined) {
    log('FATAL', 'SIDECAR_UNEXPECTED_DEVICE_ARG');
    requestRendererExit('fatal');
    return;
  }
  if (ARGS.role === 'phone' && !ARGS.device) {
    log('FATAL', 'PHONE_DEVICE_REQUIRED');
    requestRendererExit('fatal');
    return;
  }
  try {
    const ver = getSdkVersion();
    log('BOOT', `trtc-electron-sdk getSDKVersion() = ${ver}`);
  } catch (e) { /* ignore */ }

  if (ARGS.role === 'phone') {
    require('./phone').runPhone(cloud, log);
    return;
  }

  // v0.6.1：sidecar 不再进固定房间，改为受保护的控制面意图轮询驱动
  if (!config.sidecarCredential) {
    log('FATAL', 'SIDECAR_CREDENTIAL_MISSING');
    requestRendererExit('fatal');
    return;
  }
  const started = startPollingRuntime({
    runSidecar,
    pollAndJoin,
    scheduleInterval: setInterval,
    scheduleTimeout: setTimeout,
    requestFatal: () => requestRendererExit('fatal'),
    logFatal: () => log('FATAL', 'SIDECAR_INITIALIZATION_FAILED'),
  });
  if (!started) return;
  log('SIG', '意图轮询已启动（每 2s），等待手机唤醒...');
}

main();
