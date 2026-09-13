// downlink_pacer.js —— 下行注入的**实时节拍器**（2026-09-12 卡顿根因修复）
//
// 为什么必须有它
// -------------
// TRTC 的 `sendCustomAudioData` 语义是「**一次调用 = 20ms 音频，须按实时节奏调用**」。
// 而 rtc_bridge 是**突发式**下发的（实测日志 `down first push bytes=12172` ≈ 一次 19 帧），
// 原实现（rtc.js 收到即 `sendCustomAudioData`）会背靠背连发十几帧 —— SDK 只能丢弃/合并，
// 于是填充静音，听感就是「卡顿、像被截断」。
//
// 更麻烦的是：**这条路径丢帧时不报错、计数照涨**（rtc.js 里原有注释已记载该现象），
// 所以只有对落盘音频做能量分析才能发现。实测证据（同一次回复）：
//   rtc_bridge 下行 dump：135 帧 / 1 处能量悬崖（在语音结尾，正常）
//   手机收到的 wav    ：413 帧 / **3 处能量悬崖**（1200ms、1940ms 两处是凭空多出来的）
// 差额就产生在「sidecar 发送」这一段。
//
// 本模块做三件事
// --------------
//   1. `push(buf)` 只入队，**不再立刻发送**；
//   2. 一个 `frameMs`（20ms）定时器每次**只出队一帧**调用 `send` —— 一次一帧、按实时节奏；
//   3. 队列**有界**（默认 50 帧 = 1s）：溢出时**丢最旧**并计数，让「丢帧」第一次变得可观测。
//
// 依赖全部注入（timer/clear/now），因此可在无 Electron、无真实定时器的环境下确定性单测。

'use strict';

const DEFAULTS = {
  frameMs: 20,
  maxFrames: 50,
};

class DownlinkPacer {
  /**
   * @param {object} opts
   * @param {(buf: Buffer) => void} opts.send   真正注入 SDK 的函数（一次一帧）
   * @param {number} [opts.frameMs=20]          每帧时长（与链路 20ms 帧长一致）
   * @param {number} [opts.maxFrames=50]        队列上限（帧），溢出丢最旧
   * @param {(info: object) => void} [opts.onDrop] 丢帧回调（携带计数，便于打日志）
   * @param {Function} [opts.setIntervalImpl]   可注入定时器（测试用）
   * @param {Function} [opts.clearIntervalImpl]
   */
  constructor(opts) {
    if (!opts || typeof opts.send !== 'function') {
      throw new Error('DownlinkPacer 需要 send 函数');
    }
    this._send = opts.send;
    this._frameMs = opts.frameMs || DEFAULTS.frameMs;
    this._maxFrames = opts.maxFrames || DEFAULTS.maxFrames;
    this._onDrop = opts.onDrop || null;
    // 默认实现必须包一层箭头函数，**不能**直接存 `setInterval` 本身：
    // 浏览器/Electron 渲染进程里 `setInterval` 属于 `window`，脱离接收者调用会抛
    // `TypeError: Illegal invocation`（实测 2026-09-12，pacer 接线后 runSidecar 直接崩）。
    // 包一层即可保住接收者；Node 下同理无害。
    this._setInterval = opts.setIntervalImpl || ((fn, ms) => setInterval(fn, ms));
    this._clearInterval = opts.clearIntervalImpl || ((h) => clearInterval(h));

    this._queue = [];
    this._timer = null;
    // 统计：sent/dropped/maxQueue 是判断「是否还在突发注入」的直接依据；
    // underruns = 空队列 tick 次数（欠载）：节拍器跑满 20ms 但无帧可发，
    // 说明上游下行供不上（被限速/被打断/上游空窗），手机侧会听到空洞。
    // 此前欠载**从不被记录**，是「下行有洞但归因不了」的一个盲区。
    this.stats = { queued: 0, sent: 0, dropped: 0, maxQueue: 0, ticks: 0, underruns: 0 };
  }

  /** 入队一帧；超过上限则丢**最旧**的一帧（保留最近的，避免说话延迟越积越大）。 */
  push(buf) {
    this._queue.push(buf);
    this.stats.queued += 1;
    if (this._queue.length > this.stats.maxQueue) {
      this.stats.maxQueue = this._queue.length;
    }
    while (this._queue.length > this._maxFrames) {
      this._queue.shift();
      this.stats.dropped += 1;
      if (this._onDrop) {
        this._onDrop({ dropped: this.stats.dropped, queued: this._queue.length });
      }
    }
  }

  /** 一个 tick = 送一帧。返回是否真的送了（队列为空时返回 false，便于测试断言）。
   *  队列为空 = 欠载：计数到 stats.underruns（行为不变，仅新增可观测性）。 */
  tick() {
    this.stats.ticks += 1;
    const buf = this._queue.shift();
    if (buf === undefined) {
      this.stats.underruns += 1;
      return false;
    }
    this._send(buf);
    this.stats.sent += 1;
    return true;
  }

  /**
   * 立即丢弃队列中所有待发帧（**打断**用）。
   *
   * 为什么必须存在：节拍器为了消除「突发注入被 SDK 静默吞帧」而引入队列，代价是
   * **最多积压 maxFrames(50)=1 秒**待播音频。用户插话时若不清这个队列，被打断的
   * 旧回复会继续播完这 1 秒 —— 实测打断延迟因此从 ~0.7s 涨到 1.75s（自己造成的回归）。
   * 丢弃的帧计入 stats.dropped，保持可观测。
   * 注意：不清 `sent`（那是累计发送数，用于对账）。
   */
  clear() {
    const n = this._queue.length;
    if (n > 0) {
      this._queue.length = 0;
      this.stats.dropped += n;
    }
    return n;
  }

  start() {
    if (this._timer !== null) return;
    this._timer = this._setInterval(() => this.tick(), this._frameMs);
    if (this._timer && typeof this._timer.unref === 'function') {
      this._timer.unref(); // 不因它阻止进程退出（Electron 退出路径需要）
    }
  }

  stop() {
    if (this._timer === null) return;
    this._clearInterval(this._timer);
    this._timer = null;
  }

  get pending() {
    return this._queue.length;
  }
}

module.exports = { DownlinkPacer, DEFAULTS };
