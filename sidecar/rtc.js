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
const { selectPendingIntent } = require('./intent-selection');
const {
  createIntentSkipList,
  createRecoveryState,
  fetchJsonWithTimeout,
} = require('./intent-recovery');
const { CMD_ID_TERMINATE, makeTerminationCmdHandler } = require('./rtc-termination');
const { frameToS16Mono16k, makeAudioFrame16k } = require('./audio');
const { injectTestAudio } = require('./rtc-test-audio');
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
      if (action === 'exit' && !exited) {
        // 兑付失败可自愈（hello proof 过期等）：退房清会话继续轮询，不杀进程
        if (reason === 'hello_redemption_failed') {
          recoverFromRedemptionFailure();
          return;
        }
        exitSidecar(reason || 'ctrl_exit');
      }
      // E2E 测试（v0.6.4）：注入 2s 440Hz 测试音频上行 → TRTC 分发给手机端，
      // 用于验证「AI 音频 → 手机端播放」链路（手机端 DiagLog 应出现 firstAudioFrame/voiceVolume）
      if (action === 'test_audio') injectTestAudio(cloud, makeAudioFrame16k, log);
    },
    () => {
      currentRoom = null;
      log('WS', 'rtc_bridge disconnected; one-time hello discarded');
      exitSidecar('bridge_disconnected');
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
        // 2026-09-05：上行内容 RMS 判别（VOL 有能量但 bridge up rms=0 的归因分叉点）
        const now = Date.now();
        if (now - (stats._lastUpRmsLog || 0) >= 2000) {
          stats._lastUpRmsLog = now;
          let s = 0; const nS = Math.min(pcm.length >> 1, 1600);
          for (let i = 0; i < nS; i++) { const v = pcm.readInt16LE(i * 2); s += v * v; }
          log('UPRMS', `rms=${Math.sqrt(s / nS).toFixed(0)} bytes=${pcm.length} sr=${frame.sampleRate} ch=${frame.channel} vol=${frame.volume ?? 'n/a'}`);
        }
      }
    },
    onCapturedAudioFrame: null,
    onLocalProcessedAudioFrame: null,
    onMixedPlayAudioFrame: null,
    onMixedAllAudioFrame: null,
  });

  cloud.on('onEnterRoom', (result) => {
    if (result > 0) {
      log('ROOM', `进房成功（elapsed=${result}ms）`);
      // v0.6.7 P0 修复：每次进房成功后重新启用自定义音频采集。
      // 根因（2026-08-22 真机实锤）：enableCustomAudioCapture(true) 原来只在进程启动时调一次，
      // 而 TRTC SDK exitRoom 会重置采集管线——sidecar 长驻进程多次进出房后
      // sendCustomAudioData 的帧被 SDK 静默丢弃（调用不报错、计数照涨），
      // 手机端 is_playing=0、FirstAudioFrameReceived 永不触发 → 用户听不到 AI 回复。
      try { cloud.stopLocalAudio(); } catch (e) { /* ignore */ }
      try {
        cloud.enableCustomAudioCapture(true);
        log('ROOM', '自定义采集已重新启用（进房后）');
      } catch (e) {
        log('ERR', `进房后 enableCustomAudioCapture 失败: ${e.message}`);
      }
    } else {
      log('ROOM', `进房失败 errCode=${result}`);
    }
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
      currentSessionId = null; // Task #23：清对账键，此后终止通知退化为 no-op
      if (bridge) bridge.clearSession();
      try { cloud.exitRoom(); } catch (e) { /* ignore */ }
    }
  });
  cloud.on('onError', (errCode, errMsg) => log('ERR', `onError errCode=${errCode} msg=${errMsg}`));
  cloud.on('onUserAudioAvailable', (userId, available) => {
    log('AUDIO', `远端音频可用 userId=${userId} available=${available}`);
    // 回音抑制调查（2026-09-04）：setRemoteAudioVolume(userId,0) 已移除（实测连带清零
    // onPlayAudioFrame 帧数据）；setApplicationPlayVolume 在本 SDK 绑定缺失（not a function）。
    // 远端音量保持默认 100，先保上行链路，PC 扬声器抑制待换可用 API。
  });
  // 上行链路判别（2026-09-04）：onPlayAudioFrame 全零时，用 SDK 音量回调区分
  // 「上行真空（手机没发/发静音）」vs「帧回调内容被清」——SDK 音量直接反映收流能量。
  try {
    cloud.enableAudioVolumeEvaluation(500);
    cloud.on('onUserVoiceVolume', (userVolumes, userVolumesCount, totalVolume) => {
      try {
        const arr = (userVolumes || []).map(u => `${u.userId.slice(0, 8)}:${u.volume}`).join(',');
        log('VOL', `[${arr}] total=${totalVolume}`);
      } catch (e) { /* ignore */ }
    });
  } catch (e) {
    log('ERR', `enableAudioVolumeEvaluation 失败: ${e.message}`);
  }
  cloud.on('onUserSigExpired', () => {
    log('SIG', 'userSig 过期回调；由 rtc_bridge 侧重新签发后重进房（MVP 记录日志）');
  });
  // Task #23：手机端退房前的终止通知（cmdId=1）→ bridge.noteTermination 中继 rtc_bridge。
  // fail-safe：畸形消息/无 bridge 一律静默，见 rtc-termination.js。
  cloud.on('onRecvCustomCmdMsg', makeTerminationCmdHandler(
    () => bridge,
    () => currentSessionId,
    log,
  ));

  // v0.6.1：进房由意图轮询 pollAndJoin 触发（不再启动即进房）

  // 周期统计
  setInterval(() => {
    log('STAT', `up=${stats.upFrames}帧/${(stats.upBytes / 1024).toFixed(0)}KB down=${stats.downFrames}帧/${(stats.downBytes / 1024).toFixed(0)}KB ws=${bridge ? bridge.connected : false}`);
  }, 5000);

}


// ---------- 意图轮询（v0.6.1）：PC 不知道手机 device_id，枚举 pending 进对应房间 ----------
let currentRoom = null;
let currentSessionId = null; // Task #23：当前会话 ID（terminate 中继对账键；进房赋值/离开清空）
// 兑付失败自愈状态（2026-09-05）：意图判死列表 + 连续失败计数
const skippedIntents = createIntentSkipList();
const redemptionRecovery = createRecoveryState();
let redemptionRecoveryTimer = null;
const RECOVERY_OBSERVATION_MS = 20000;

// 任一会话存活超过观察窗（兑付成功且未被 ctrl exit 打断）即重置连续失败计数
function scheduleRecoveryReset() {
  if (redemptionRecoveryTimer) clearTimeout(redemptionRecoveryTimer);
  redemptionRecoveryTimer = setTimeout(() => {
    redemptionRecoveryTimer = null;
    redemptionRecovery.reset();
  }, RECOVERY_OBSERVATION_MS);
}

// hello proof TTL=60s：backend 事件循环阻塞/网络抖动都可能让 proof 在发送前
// 过期（40112）。此时意图已消费、重签必被拒——判死该意图，退房清会话继续
// 轮询；用户再次「立即监听」产生新会话即可恢复。连续失败达上限才退出防崩溃循环。
function recoverFromRedemptionFailure() {
  if (redemptionRecoveryTimer) { clearTimeout(redemptionRecoveryTimer); redemptionRecoveryTimer = null; }
  skippedIntents.add(currentSessionId);
  const keepGoing = redemptionRecovery.recordFailure();
  log('RECOVERY', `兑付失败自愈: 连续第 ${redemptionRecovery.consecutiveFailures} 次（意图 ${currentSessionId} 已判死）`);
  try { cloud.exitRoom(); } catch (e) { /* ignore */ }
  if (bridge) bridge.clearSession();
  currentRoom = null;
  currentSessionId = null;
  if (!keepGoing) {
    exitSidecar('hello_redemption_failed_exhausted');
  }
}
let pollingBusy = false;

async function fetchSigForDevice(intent) {
  // sign_for_sidecar 会消费意图（防重复进房），返回同一房间的 PC userSig
  const parsed = await fetchJsonWithTimeout(`${ARGS.signUrl}/api/v1/voice/session/sign`, {
    method: 'POST',
    headers: controlPlaneHeaders({ credential: config.sidecarCredential }),
    body: JSON.stringify({
      session_id: intent.session_id,
      claim_token: intent.claim_token,
      device_id: intent.device_id,
      user_id: config.SIDECAR_USER_ID,
    }),
  });
  if (parsed.code === 0 && parsed.data && parsed.data.user_sig) {
    log('SIG', '意图消费成功');
    return parsed.data;
  }
  // 确定性拒绝（如意图已被消费 40901）→ 调用方应把该意图判死进跳过列表；
  // err.definitive=false 的网络/超时类异常不判死（意图仍有效，下轮可重试）
  const err = new Error(`sign failed code=${Number(parsed.code) || 50300}`);
  err.definitive = true;
  throw err;
}

async function pollAndJoin() {
  if (exited || pollingBusy) return;
  pollingBusy = true;
  let selectedIntent = null;
  try {
    const parsed = await fetchJsonWithTimeout(`${ARGS.signUrl}/api/v1/voice/session/pending`, {
      method: 'GET',
      headers: controlPlaneHeaders({ credential: config.sidecarCredential }),
    });
    const intents = (parsed.data && parsed.data.intents) || [];
    if (intents.length === 0) { pollingBusy = false; return; }
    const intent = selectPendingIntent(intents, currentRoom, skippedIntents);
    if (!intent) { pollingBusy = false; return; }
    selectedIntent = intent;
    log('SIG', '发现会话意图');
    if (currentRoom) {
      try { cloud.exitRoom(); } catch (e) { /* ignore */ }
      await new Promise(r => setTimeout(r, 600));
    }
    const cred = await fetchSigForDevice(intent);
    if (cred.room_id !== intent.room_id) {
      if (bridge) bridge.clearSession();
      throw new Error('SIDECAR_SESSION_ROOM_MISMATCH');
    }
    bridge.startSession(cred.hello);
    currentRoom = cred.room_id;
    currentSessionId = cred.hello.session_id; // Task #23：终止通知对账键
    enterRoom(cred);
    scheduleRecoveryReset();
  } catch (e) {
    if (e && e.definitive && selectedIntent) {
      skippedIntents.add(selectedIntent.session_id);
      log('ERR', `意图判死: ${selectedIntent.session_id}（${e.message}）`);
    }
    log('ERR', `意图轮询失败: ${e.message}`);
  }
  pollingBusy = false;
}
function exitSidecar(reason) {
  if (exited) return;
  exited = true;
  log('ROOM', `退出 sidecar（reason=${reason}）`);
  currentSessionId = null; // Task #23：进程退出前清对账键
  try { cloud.exitRoom(); } catch (e) { /* ignore */ }
  if (bridge) bridge.close();
  setTimeout(() => requestRendererExit('controlled'), 400);
}

function getSdkVersion() {
  try { return cloud.getSDKVersion(); } catch (e) { return 'unknown'; }
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
