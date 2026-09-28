'use strict';

// desktop-loop.js —— 电脑自己说话，不依赖手机，也不依赖本机 19092。
//
// 本进程扮演「说话的那一端」。云端 jax-pc-sidecar 继续扮演「模型那一端」：
//   设备凭证 POST /voice/session（入队，供云端领取）
//   → 本机用同一份凭证进同一间房
//   → startLocalAudio 把电脑麦克风送进房间
//   → 房间里的模型回复由 TRTC 播放到本机喇叭
// 禁止 sendCustomAudioData：它和本机麦克风互斥。
// 禁止连接 127.0.0.1:19092：这台电脑上没有本地语音桥，桥在云端容器里。

const config = require('./config');
const { controlPlaneHeaders } = require('./security');
const { TRTCParams, TRTCAppScene, TRTCAudioQuality } = require('trtc-electron-sdk');
const { requestRendererExit } = require('./exit-protocol');

const { ARGS } = config;

function deviceCredentialPath() {
  const base = process.env.APPDATA || require('os').tmpdir();
  return require('path').join(base, 'com.jax.pet', 'desktop-device-credential');
}

async function ensureDesktopCredential(fetchImpl, log) {
  if (process.env.VOICE_DESKTOP_DEVICE_CREDENTIAL) {
    return process.env.VOICE_DESKTOP_DEVICE_CREDENTIAL;
  }
  const fs = require('fs');
  const file = deviceCredentialPath();
  try {
    const saved = fs.readFileSync(file, 'utf8').trim();
    if (saved.startsWith(`${ARGS.device}.`) && saved.length >= ARGS.device.length + 33) {
      process.env.VOICE_DESKTOP_DEVICE_CREDENTIAL = saved;
      return saved;
    }
  } catch (_) { /* first launch */ }
  const owner = process.env.VOICE_OWNER_CREDENTIAL || '';
  if (!owner) throw new Error('本机管理员凭证缺失');
  const pairing = await fetchImpl(`${ARGS.signUrl}/api/v1/voice/devices/pairing-code`, {
    method: 'POST',
    headers: controlPlaneHeaders({ credential: owner }),
    body: JSON.stringify({ platform: 'windows', device_name_hint: 'this-pc' }),
  });
  const pairingBody = await pairing.json();
  const code = pairingBody && pairingBody.data && pairingBody.data.pairing_code;
  if (!code) throw new Error('本机配对码签发失败');
  const registered = await fetchImpl(`${ARGS.signUrl}/api/v1/voice/devices/register`, {
    method: 'POST',
    headers: controlPlaneHeaders({ credential: owner }),
    body: JSON.stringify({
      pairing_code: code,
      device_name: 'this-pc',
      platform: 'windows',
      device_id: ARGS.device,
    }),
  });
  const registeredBody = await registered.json();
  const data = registeredBody && registeredBody.data;
  if (!data || data.device_id !== ARGS.device || !data.credential_secret) {
    throw new Error('本机设备注册失败');
  }
  const credential = `${data.device_id}.${data.credential_secret}`;
  fs.mkdirSync(require('path').dirname(file), { recursive: true });
  fs.writeFileSync(file, credential, { encoding: 'utf8', mode: 0o600 });
  process.env.VOICE_DESKTOP_DEVICE_CREDENTIAL = credential;
  log('DESKTOP', '本机设备已注册');
  return credential;
}

function runDesktop(cloud, log, deps = {}) {
  const fetchImpl = deps.fetch || global.fetch;

  async function fetchDesktopSession() {
    const credential = await ensureDesktopCredential(fetchImpl, log);
    const resp = await fetchImpl(`${ARGS.signUrl}/api/v1/voice/session`, {
      method: 'POST',
      headers: controlPlaneHeaders({ credential }),
      body: JSON.stringify({ device_id: ARGS.device, entry_point: 'main' }),
    });
    const parsed = await resp.json();
    if (parsed && parsed.code === 0 && parsed.data && parsed.data.user_sig) return parsed.data;
    const code = parsed && typeof parsed.code === 'number' ? parsed.code : resp.status;
    log('DESKTOP', `签发失败 code=${code}`);
    throw new Error('本机会话签发失败');
  }

  function enterRoom(cred) {
    const params = new TRTCParams();
    params.sdkAppId = Number(cred.sdk_app_id);
    params.userId = cred.user_id;
    params.userSig = cred.user_sig;
    params.strRoomId = cred.room_id;
    log('DESKTOP', `进房 room=${cred.room_id} user=${cred.user_id}`);
    cloud.enterRoom(params, TRTCAppScene.TRTCAppSceneAudioCall);
  }

  function armLocalMic() {
    try { cloud.enableCustomAudioCapture(false); } catch (_) { /* already off */ }
    try { cloud.stopLocalAudio(); } catch (_) { /* not started */ }
    cloud.startLocalAudio(TRTCAudioQuality.TRTCAudioQualitySpeech);
    log('DESKTOP', '本机麦克风已开启，回复由房间播放到喇叭');
  }

  cloud.on('onEnterRoom', (result) => {
    if (result > 0) {
      log('DESKTOP', `进房成功 elapsed=${result}`);
      armLocalMic();
    } else {
      log('DESKTOP', `进房失败 errCode=${result}`);
    }
  });
  cloud.on('onRemoteUserEnterRoom', (userId) => {
    log('DESKTOP', `模型端已进房 userId=${userId}`);
  });
  cloud.on('onError', (errCode, errMsg) => {
    log('ERR', `desktop onError errCode=${errCode} msg=${errMsg}`);
  });

  fetchDesktopSession()
    .then((cred) => enterRoom(cred))
    .catch((error) => {
      log('FATAL', `DESKTOP_SESSION_FAILED ${error.message}`);
      requestRendererExit('fatal');
    });
}

module.exports = { runDesktop };
