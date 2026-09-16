'use strict';

// adev.js —— 「本端的音频设备表」清点（纯函数，无原生依赖，可在 Node 下直接单测）
//
// 为什么需要它（2026-09-16 线上事故，容器实跑）
// -------------------------------------------
// 那次事故的表现是 sidecar 侧 `[STAT] up=0帧` **连续 70 秒**、`[PCM]`/`[UPRMS]` 一行都没有，
// 而手机在正常说话（`RtcCustomAudio: lvl raw=64 … gate=true`）。也就是说：
// 「手机在发」与「对端没收到」两件事同时成立，但**我们没有任何一行日志说明为什么**。
//
// 真因在 TRTC 原生层，而在我们自己的日志里是空的：
//
//     [I][…][audio_player_safe_wrapper.cc:384] Player error player device list is empty
//                                              with system error:-12.  msg: player device list is empty
//     [W][…][audio_event_dispatcher.cc:272] OnWarning [code:1202|message:player device list is empty|io_source:player]
//     [I][…][io_working_status_printer.cc:107] Within 40000 ms, kPlayout produced 0 ms data, callback count is 0
//     [I][…][rtc_audio_jitter_buffer_v2.cc:484] PacketBuffer is full, drop frame count: 12 … io_last_read_frame_ticks: -1
//
// 这四行合起来说明了一条完备的因果链：**本端没有可用的播放设备 ⇒ 播放拉取链路根本没启动
// （jitter buffer 从未被读过，io_last_read_frame_ticks 恒为 -1）⇒ 远端音频帧永远不会回调给
// 应用（`onPlayAudioFrame` 不触发，这正是 up=0 的唯一含义）**。它不是网络问题、不是订阅问题、
// 更不是手机没发。
//
// 但上面四行都是 TRTC 自己的原生日志，只在 `--enable-logging=stderr` 打开时才可见，
// 且不带我们的作用域、也进不了 `/status.sidecar.events` 的语义过滤。本模块把那件事
// **翻译成我们自己的、可检索的一行**：直接问 SDK 要设备表（`getSpeakerDevicesList` /
// `getMicDevicesList`），并在 1202 警告到来时把「设备表为空」的后果一起写清楚。
//
// 口径（务必保持）
// --------------
// * 这里问的是 **TRTC 自己的设备表**，不是 pulseaudio 的设备表。两者的差异本身就是判据：
//   PA 里有 sink 而 TRTC 的表为空 ⇒ 问题在 SDK 的设备筛选/打开；PA 里就没有 sink ⇒ 问题在音频子系统。
// * 清点失败**绝不抛**：观测代码不能成为新的故障面（fail-open）。

// TRTC 警告码：播放设备列表为空（`io_source:player`）。容器里没有可用播放设备时必现。
const PLAYER_DEVICE_EMPTY_CODE = 1202;

/** 取一台设备的可读名：设备名缺失时退化为 deviceId，都没有则占位。 */
function deviceLabel(entry) {
  if (!entry || typeof entry !== 'object') return '';
  const name = typeof entry.deviceName === 'string' ? entry.deviceName : '';
  const id = typeof entry.deviceId === 'string' ? entry.deviceId : '';
  return name || id || '<unnamed>';
}

/**
 * 问 SDK 要某个方向的设备表。
 * @param {object} cloud TRTCCloud 单例
 * @param {string} method 'getSpeakerDevicesList' | 'getMicDevicesList'
 * @returns {{names: string[], error: string}} 失败时 names 为空、error 非空
 */
function listDevices(cloud, method) {
  if (!cloud || typeof cloud[method] !== 'function') {
    return { names: [], error: `${method} 不可用` };
  }
  try {
    const list = cloud[method]();
    if (!Array.isArray(list)) return { names: [], error: `${method} 返回非数组` };
    return { names: list.map(deviceLabel), error: '' };
  } catch (e) {
    return { names: [], error: e && e.message ? e.message : String(e) };
  }
}

/**
 * 清点本端音频设备表。
 * @returns {{speakers: string[], mics: string[], speakersError: string, micsError: string}}
 */
function inventory(cloud) {
  const speakers = listDevices(cloud, 'getSpeakerDevicesList');
  const mics = listDevices(cloud, 'getMicDevicesList');
  return {
    speakers: speakers.names,
    mics: mics.names,
    speakersError: speakers.error,
    micsError: mics.error,
  };
}

function joinNames(names) {
  if (!names.length) return '空';
  return names.join(',');
}

/** 设备表 → 一行可检索文本（`[ADEV]` 作用域，进事件环）。 */
function formatInventory(inv) {
  const speakers = Array.isArray(inv && inv.speakers) ? inv.speakers : [];
  const mics = Array.isArray(inv && inv.mics) ? inv.mics : [];
  const parts = [
    `speakers=${speakers.length}(${joinNames(speakers)})`,
    `mics=${mics.length}(${joinNames(mics)})`,
  ];
  const errors = [inv && inv.speakersError, inv && inv.micsError].filter(Boolean);
  if (errors.length) parts.push(`清点异常=${errors.join(';')}`);
  if (!speakers.length) {
    // 播放设备为空是本端**唯一**会让「手机在发、对端收不到」同时成立的条件，
    // 所以这里必须显式点出来，而不是让人从 speakers=0 自己推。
    parts.push('无播放设备 ⇒ TRTC 播放管线不会启动，远端帧不会回调（onPlayAudioFrame），上行恒为 0');
  }
  return parts.join(' ');
}

/**
 * `onWarning` → 一行归因。
 * @param {number} code
 * @param {string} message
 * @param {object|null} inv 警告发生**当下**的设备表（可为 null，则不含计数）
 * @returns {{audioDevice: boolean, text: string}}
 *   audioDevice=true 时应以 `[ADEV]` 作用域落盘（便于与普通警告分开检索）。
 */
function onWarningLine(code, message, inv) {
  const msg = typeof message === 'string' ? message : String(message === undefined ? '' : message);
  const audioDevice = Number(code) === PLAYER_DEVICE_EMPTY_CODE;
  const text = `onWarning errCode=${Number(code)} msg=${msg}`;
  if (!audioDevice) return { audioDevice: false, text };
  const invText = inv ? ` ${formatInventory(inv)}` : '';
  return {
    audioDevice: true,
    text: `${text}${invText} ⇒ 本端无可用播放设备：远端音频帧不会回调，上行必为 0（判据：/status.audio.devices 与 /status.sidecar.events 的 [ADEV] 行）`,
  };
}

module.exports = {
  PLAYER_DEVICE_EMPTY_CODE,
  inventory,
  formatInventory,
  onWarningLine,
};
