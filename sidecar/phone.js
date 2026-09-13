// phone.js —— 联调用手机模拟器（TRTC 对端，非生产代码）
//
// 角色 = 真实手机：进房 jax-<device_id>（userId=device_id）→ 推 wav 音频上行 →
// 收 sidecar 注入的回复音频（onPlayAudioFrame）→ 写回复 wav。
// 由 rtc.js 在 --role=phone 时调用；用真实 TRTC SDK 走完整链路（不经 rtc_bridge 直连）。
const config = require('./config');
const { frameToS16Mono16k, splitIntoFrames, writeWav16k, readWav16k } = require('./audio');
const { TRTCParams, TRTCAppScene } = require('trtc-electron-sdk');
const { requestRendererExit } = require('./exit-protocol');
const { controlPlaneHeaders } = require('./security');

const { ARGS } = config;
const SIDECAR_USER_ID = 'jax-pc-sidecar';

function runPhone(cloud, log) {
  const stats = { upFrames: 0, replyFrames: 0, replyBytes: 0,
                  replySpeechFrames: 0, replySpeechBytes: 0 };
  const replyParts = [];
  const frameGaps = [];          // 相邻回复帧的到达间隔（ms）——量化卡顿的唯一办法
  let firstReplyTs = null;
  let lastReplyTs = null;
  let lastSpeechTs = null;       // 最近一次「有有效能量」的时刻：结束判定必须用它
  // ── 打断（barge-in）测量：模型说到一半时手机插话，量「插话 → 模型停止输出」的延迟 ──
  // 这是三项体验里此前**完全没测过**的一项（流畅度/完整度已有指标）。
  let bargeInSentTs = null;          // 插话首帧发出的时刻
  let lastSpeechAfterBargeTs = null; // 插话后仍听到语音的最后一刻（含**新回复**，会高估）
  let gapStartTs = null;             // 插话后第一个静音间隙的起点（累计中）
  let oldReplyEndTs = null;          // **旧回复结束**的时刻（主口径）
  let bargeInStarted = false;
  let upStartTs = null;
  let remoteReadyTs = null;
  let exited = false;
  let firstFrameMetaLogged = false;
  let firstPcmLogged = false;

  async function fetchPhoneSig() {
    // 真实设备凭证只从环境变量读入；用与云端 sidecar 相同的控制面头构造器
    // （Bearer + 一次性 nonce），否则服务端 40101/40102 直接拒绝。
    const credential = process.env.VOICE_SIM_DEVICE_CREDENTIAL || '';
    const resp = await fetch(`${ARGS.signUrl}/api/v1/voice/session`, {
      method: 'POST',
      headers: controlPlaneHeaders({ credential }),
      body: JSON.stringify({ device_id: ARGS.device, entry_point: 'main' }),
    });
    let parsed = null;
    try {
      parsed = await resp.json();
    } catch (e) {
      parsed = null; // 非 JSON 错误体：响应原文不得进入异常/日志
    }
    if (parsed && parsed.code === 0 && parsed.data && parsed.data.user_sig) return parsed.data;
    const code = parsed && typeof parsed.code === 'number' ? parsed.code : resp.status;
    log('PHONE', `签发失败 code=${code}`);
    throw new Error('手机签发失败'); // 不携带凭证/响应原文，防止泄漏
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

  // 等待 sidecar 对端进房：云端 sidecar 每 2s 轮询 /session/pending 才进房，
  // 若不等就推流，会在对端不在房间时把语音推完——测不出真实指标且丢首包。
  async function waitRemoteReady() {
    const graceMs = ARGS.joinGraceS * 1000;
    const pollStartTs = Date.now();
    while (remoteReadyTs === null && Date.now() - pollStartTs < graceMs) {
      if (exited) return;
      await sleep(50);
    }
    if (remoteReadyTs !== null) {
      log('PHONE', `远端就绪 @${Date.now() - pollStartTs}ms`);
      return;
    }
    // 不能死等：超时后仍继续上行，否则整个流程拿不到任何指标
    log('PHONE', `远端未就绪（${graceMs}ms 超时，继续上行）`);
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
    await waitRemoteReady();
    upStartTs = Date.now(); // first_reply_ms 口径基准 = 真正开始上行第一帧
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

  // ── 打断（barge-in）测量 ────────────────────────────────────────────────────
  // 三项体验里此前完全没数据的一项。做法：首包回复后 delayMs 推一段**人声**上行，
  // 模拟用户插话；再记录「插话之后模型仍在出声」的最后一刻，两者之差即停止延迟。
  // 参数走环境变量（不新增 CLI 白名单，与凭证/日志目录同风格）。
  function scheduleBargeIn() {
    const wavPath = process.env.SIM_BARGE_IN_WAV || '';
    if (!wavPath || bargeInStarted) return;
    const delayMs = Number(process.env.SIM_BARGE_IN_AFTER_MS || 800);
    setTimeout(() => { pushBargeIn(wavPath).catch(() => {}); }, Math.max(0, delayMs));
  }

  async function pushBargeIn(wavPath) {
    if (bargeInStarted || exited) return;
    let pcm;
    try {
      pcm = readWav16k(wavPath);
    } catch (e) {
      log('PHONE', `打断 wav 读取失败: ${e.message}`);
      return;
    }
    bargeInStarted = true;
    bargeInSentTs = Date.now();
    const frames = splitIntoFrames(pcm, 20);
    log('PHONE', `打断上行开始（${frames.length}帧）`);
    for (const f of frames) {
      if (exited) return;
      try { cloud.sendCustomAudioData(makePhoneFrame(f)); } catch (e) { break; }
      await sleep(20);
    }
    log('PHONE', '打断上行结束');
  }

  // 下行：收回复音频 → 累积 → 写 wav
  function setupDownlink() {
    cloud.setAudioFrameCallback({
      onPlayAudioFrame: (frame, userId) => {
        if (!frame || !frame.data) return;
        if (userId !== SIDECAR_USER_ID) return; // 只收 sidecar 回复
        // 首帧把**真实**采样率/声道/长度打出来：若 frame.sampleRate 缺失或为假值，
        // audio.js 会走「16k 直通」分支，把 48k 样本写进 16k 的 wav
        // → 播放快 3 倍、音调高 3 倍（听感「3 倍语速、根本听不清」）。
        if (!firstFrameMetaLogged) {
          firstFrameMetaLogged = true;
          const rawLen = frame.data.length || frame.data.byteLength || 0;
          log('PHONE', `回复首帧元数据: sampleRate=${frame.sampleRate} channel=${frame.channel} `
            + `length=${frame.length} rawBytes=${rawLen}`);
        }
        const pcm = frameToS16Mono16k(frame);
        if (pcm && !firstPcmLogged) {
          firstPcmLogged = true;
          log('PHONE', `回复首帧转换后: ${pcm.length} bytes = ${pcm.length / 2 / 16000 * 1000}ms@16k`);
        }
        if (!pcm || pcm.length === 0) return;
        if (firstReplyTs === null) {
          firstReplyTs = Date.now();
          const latency = upStartTs ? firstReplyTs - upStartTs : 0;
          log('PHONE', `首包回复 @${latency}ms（自上行开始）`);
          scheduleBargeIn();   // 打断测量：首包到达后再安排插话
        } else if (lastReplyTs !== null) {
          frameGaps.push(Date.now() - lastReplyTs);
        }
        lastReplyTs = Date.now();
        if (pcm && pcm.length > 0) {
          // 有效能量判定：模型说完后 TRTC 仍会持续送**全零静音帧**（实测长达 5.9s）。
          // 因此「回复结束」不能按「有没有帧到达」判，必须按**是否还有有效能量**判，
          // 否则结束判定被零帧无限推迟；同理 reply_frames 会把静音也算成回复内容。
          let acc = 0;
          const n = pcm.length >> 1;
          for (let i = 0; i < n; i += 4) { const v = pcm.readInt16LE(i * 2); acc += v * v; }
          const rms = Math.sqrt(acc / Math.max(1, Math.ceil(n / 4)));
          if (rms >= SPEECH_RMS) {
            stats.replySpeechFrames += 1;
            stats.replySpeechBytes += pcm.length;
            lastSpeechTs = Date.now();
            // 插话已发出后：继续记录「模型还在说」的最后一刻，以及
            // **旧回复结束**的时刻（插话后第一个持续静音间隙的起点）。
            // ⚠️ 为什么必须区分：打断之后模型会开始回应插话，那些语音也是「含语音能量的帧」。
            // 用「最后一帧语音」当打断延迟，会把**新回复**算成「旧回复还没停」——系统性高估。
            if (bargeInSentTs !== null) {
              lastSpeechAfterBargeTs = Date.now();
              if (gapStartTs !== null) {
                if (oldReplyEndTs === null) oldReplyEndTs = gapStartTs;  // 间隙已成立
                gapStartTs = null;
              }
            }
          } else if (bargeInSentTs !== null) {
            // 非语音帧：若在插话后已出现过语音，则开始累计静音间隙
            if (lastSpeechAfterBargeTs !== null && gapStartTs === null) gapStartTs = Date.now();
          }
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
    cloud.on('onRemoteUserEnterRoom', (userId) => {
      log('PHONE', `远端加入 ${userId}`);
      if (userId === SIDECAR_USER_ID) remoteReadyTs = Date.now(); // 对端已进房，可开始上行
    });
    cloud.on('onRemoteUserLeaveRoom', (userId) => log('PHONE', `远端离开 ${userId}`));
    cloud.on('onEnterRoom', (result) => log('PHONE', result > 0 ? `进房成功 ${result}ms` : `进房失败 ${result}`));
    enterRoom(cred);
    await pushWav(cred);

    // hold 超时 / 或收到回复后保持一小段再退出
    const waitReply = await waitForReply();
    if (waitReply) {
      log('PHONE', `回复结束 ${stats.replyFrames}帧/${(stats.replyBytes / 1024).toFixed(0)}KB`
        + `（连续 ${REPLY_TAIL_MS}ms 无新帧判定说完）`);
      if (frameGaps.length > 0) {
        const sorted = [...frameGaps].sort((a, b) => a - b);
        const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
        // 帧长 20ms：间隔 >40ms 就已经听得出停顿（卡顿）
        const rough = frameGaps.filter((g) => g > 40).length;
        log('PHONE', `帧间隔 p50=${at(0.5)}ms p95=${at(0.95)}ms max=${sorted[sorted.length - 1]}ms`
          + ` 超40ms的间隔=${rough}/${frameGaps.length}`);
      }
      await sleep(300);
    } else {
      log('PHONE', `hold=${ARGS.holdS}s 内未收到回复（或超时），退出`);
    }
    exitPhone();
  }

  // 回复「说完了」的判定：连续 REPLY_TAIL_MS 没有新帧即视为结束。
  // 旧逻辑是「收到第一帧后再等 2s 就退出」——那会把还没说完的回复直接砍断：
  // 实测收回的音频正好 2.64s（≈2s + 首帧前的零头），用户听感就是「不完整」。
  const REPLY_TAIL_MS = 1200;
  // 有效语音能量门限（s16 RMS）。低于它视为静音/底噪：不计入「说出的话」，
  // 也不刷新结束计时 —— 否则模型说完后 TRTC 送的零帧会让结束判定永不触发。
  const SPEECH_RMS = 60;

  function waitForReply() {
    return new Promise((resolve) => {
      const deadline = Date.now() + (ARGS.holdS * 1000);
      const iv = setInterval(() => {
        const now = Date.now();
        if (lastSpeechTs !== null && now - lastSpeechTs >= REPLY_TAIL_MS) {
          clearInterval(iv);
          resolve(true);                       // 有效语音已结束（不是「帧停了」）
        } else if (now > deadline) {
          clearInterval(iv);
          resolve(replyParts.length > 0);      // 兜底：hold 到顶
        }
      }, 100);
    });
  }

  function exitPhone() {
    if (exited) return;
    exited = true;
    const out = ARGS.outWav
      || ((process.env.JAX_SIDECAR_LOG_DIR || (__dirname + '/logs')) + '/phone_reply.wav');
    if (replyParts.length > 0) {
      try {
        writeWav16k(out, replyParts);
        log('PHONE', `回复已保存: ${out}（${stats.replyBytes}B）`);
      } catch (e) {
        log('PHONE', `写回复 wav 失败: ${e.message}`);
      }
    }
    log('PHONE', `有效语音 ${stats.replySpeechFrames}帧/${stats.replySpeechBytes}B`
      + `（共收到 ${stats.replyFrames}帧/${stats.replyBytes}B，含静音帧）`);
    if (bargeInSentTs !== null) {
      // 主口径：插话 → **旧回复结束**（插话后第一个持续静音间隙的起点）。
      // 为什么不用「最后一帧语音」：打断后模型会回应插话，那些语音会被误算成「还没停」。
      const oldEnd = oldReplyEndTs === null ? null : oldReplyEndTs - bargeInSentTs;
      log('PHONE', `打断停止(旧回复结束) @${oldEnd === null ? 'n/a' : `${oldEnd}ms`}`);
      // 参考口径：插话 → 最后一帧语音（**含新回复**，必然高估，仅作对照）
      const ref = lastSpeechAfterBargeTs === null ? null : lastSpeechAfterBargeTs - bargeInSentTs;
      log('PHONE', `打断参考(含新回复语音) @${ref === null ? 'n/a' : `${ref}ms`}`);
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
