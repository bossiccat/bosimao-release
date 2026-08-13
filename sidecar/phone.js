// phone.js —— 联调用手机模拟器（TRTC 对端，非生产代码）
//
// 角色 = 真实手机：进房 jax-<device_id>（userId=device_id）→ 推 wav 音频上行 →
// 收 sidecar 注入的回复音频（onPlayAudioFrame）→ 写回复 wav。
// 由 rtc.js 在 --role=phone 时调用；用真实 TRTC SDK 走完整链路（不经 rtc_bridge 直连）。
const config = require('./config');
const { frameToS16Mono16k, splitIntoFrames, writeWav16k, readWav16k } = require('./audio');
const { TRTCParams, TRTCAppScene } = require('trtc-electron-sdk');
const { requestRendererExit } = require('./exit-protocol');

const { ARGS } = config;
const SIDECAR_USER_ID = 'jax-pc-sidecar';

function runPhone(cloud, log) {
  const stats = { upFrames: 0, replyFrames: 0, replyBytes: 0 };
  const replyParts = [];
  let firstReplyTs = null;
  let upStartTs = null;
  let exited = false;

  async function fetchPhoneSig() {
    const resp = await fetch(`${ARGS.signUrl}/api/v1/voice/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: ARGS.device }),
    });
    const parsed = await resp.json();
    if (parsed.code === 0 && parsed.data && parsed.data.user_sig) return parsed.data;
    throw new Error(`手机签发失败: ${JSON.stringify(parsed)}`);
  }

  function enterRoom(cred) {
    const params = new TRTCParams();
    params.sdkAppId = Number(cred.sdk_app_id);
    params.userId = cred.user_id;
    params.userSig = cred.user_sig;
    params.strRoomId = cred.room_id;
    log('PHONE', `进房 room=${cred.room_id} user=${cred.user_id}`);
    cloud.enterRoom(params, TRTCAppScene.TRTCAppSceneAudioCall);
  }

  // 上行：推 16k s16 wav + 尾部静音（VAD 说完判定），20ms 真实节拍
  async function pushWav(cred) {
    if (!ARGS.wav) {
      log('PHONE', '未指定 --wav，仅进房等待回复');
      return;
    }
    let pcm;
    try {
      pcm = readWav16k(ARGS.wav);
    } catch (e) {
      log('PHONE', `读取 wav 失败: ${e.message}`);
      return;
    }
    log('PHONE', `wav=${ARGS.wav} ${pcm.length}B（16k s16）`);
    upStartTs = Date.now();
    const frames = splitIntoFrames(pcm, 20);   // 20ms 帧
    for (const f of frames) {
      if (exited) return;
      try {
        cloud.sendCustomAudioData(makePhoneFrame(f));
        stats.upFrames += 1;
      } catch (e) {
        log('ERR', `phone sendCustomAudioData 失败: ${e.message}`);
        break;
      }
      await sleep(20); // 20ms 真实节拍
    }
    // 尾部 2s 静音（对齐 apm_bridge 停顿补静音语义，触发模型说完）
    log('PHONE', `wav 推完（${stats.upFrames}帧），补 2s 静音`);
    const silence = Buffer.alloc(16000 * 2 * 2); // 2s @16k mono s16
    const silFrames = splitIntoFrames(silence, 20);
    for (const f of silFrames) {
      if (exited) return;
      try { cloud.sendCustomAudioData(makePhoneFrame(f)); } catch (e) { break; }
      await sleep(20);
    }
    log('PHONE', '上行完成，等待回复…');
  }

  // 下行：收回复音频 → 累积 → 写 wav
  function setupDownlink() {
    cloud.setAudioFrameCallback({
      onPlayAudioFrame: (frame, userId) => {
        if (!frame || !frame.data) return;
        if (userId !== SIDECAR_USER_ID) return; // 只收 sidecar 回复
        const pcm = frameToS16Mono16k(frame);
        if (!pcm || pcm.length === 0) return;
        if (firstReplyTs === null) {
          firstReplyTs = Date.now();
          const latency = upStartTs ? firstReplyTs - upStartTs : 0;
          log('PHONE', `首包回复 @${latency}ms（自上行开始）`);
        }
        replyParts.push(pcm);
        stats.replyFrames += 1;
        stats.replyBytes += pcm.length;
      },
      onCapturedAudioFrame: null,
      onLocalProcessedAudioFrame: null,
      onMixedPlayAudioFrame: null,
      onMixedAllAudioFrame: null,
    });
  }

  async function main() {
    setupDownlink();
    let cred;
    try {
      cred = await fetchPhoneSig();
    } catch (e) {
      log('PHONE', 'PHONE_SESSION_SIGN_FAILED');
      requestRendererExit('fatal');
      return;
    }
    try { cloud.stopLocalAudio(); } catch (e) { /* ignore */ }
    try { cloud.enableCustomAudioCapture(true); } catch (e) { log('ERR', `enableCustomAudioCapture: ${e.message}`); }
    cloud.on('onRemoteUserEnterRoom', (userId) => log('PHONE', `远端加入 ${userId}`));
    cloud.on('onRemoteUserLeaveRoom', (userId) => log('PHONE', `远端离开 ${userId}`));
    cloud.on('onEnterRoom', (result) => log('PHONE', result > 0 ? `进房成功 ${result}ms` : `进房失败 ${result}`));
    enterRoom(cred);
    await pushWav(cred);

    // hold 超时 / 或收到回复后保持一小段再退出
    const holdMs = ARGS.holdS * 1000;
    const waitReply = await waitForReply();
    if (waitReply) {
      log('PHONE', `收到回复 ${stats.replyFrames}帧/${(stats.replyBytes / 1024).toFixed(0)}KB，2s 后退出`);
      await sleep(2000);
    } else {
      log('PHONE', `hold=${ARGS.holdS}s 内未收到回复（或超时），退出`);
    }
    exitPhone();
  }

  function waitForReply() {
    return new Promise((resolve) => {
      const deadline = Date.now() + (ARGS.holdS * 1000);
      const iv = setInterval(() => {
        if (replyParts.length > 0) {
          clearInterval(iv);
          resolve(true);
        } else if (Date.now() > deadline) {
          clearInterval(iv);
          resolve(false);
        }
      }, 200);
    });
  }

  function exitPhone() {
    if (exited) return;
    exited = true;
    const out = ARGS.outWav || (__dirname + '/logs/phone_reply.wav');
    if (replyParts.length > 0) {
      try {
        writeWav16k(out, replyParts);
        log('PHONE', `回复已保存: ${out}（${stats.replyBytes}B）`);
      } catch (e) {
        log('PHONE', `写回复 wav 失败: ${e.message}`);
      }
    }
    log('PHONE', `上行 ${stats.upFrames}帧 / 回复 ${stats.replyFrames}帧`);
    try { cloud.exitRoom(); } catch (e) { /* ignore */ }
    setTimeout(() => requestRendererExit('controlled'), 400);
  }

  main().catch(() => {
    log('PHONE', 'PHONE_RUNTIME_FATAL');
    requestRendererExit('fatal');
  });
}

function makePhoneFrame(buf) {
  // Task 9：TRTCAudioFrame 构造只允许在 audio.js（SPEC §4.3 唯一格式 adapter）；
  // 实际 SDK 契约支持 sampleRate=16000（d.ts 实测），16k 直接注入
  const { makeAudioFrame16k } = require('./audio');
  return makeAudioFrame16k(buf);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

module.exports = { runPhone };
