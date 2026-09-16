package com.jax.voice.net

/**
 * 上行采集「静音看门狗」（2026-09-16，商业化 C4：fail-loud）。
 *
 * 为什么必须有
 * ------------
 * 真机实测事故：上行采集**恒为零**的情况下，应用照旧显示 IN_ROOM/聆听中，**不报任何错误** ——
 * 用户唯一的发现方式是"说话没反应"。这在商业产品里比采集失败本身更糟：
 * 用户无法判断、无法报修，客服只会说"重启试试"。
 *
 * 本类把「连续读到零电平」变成一个**显式事件**：采集循环每帧把 RMS 喂进来，
 * 连续零电平达到阈值时触发一次回调（并复位，避免刷屏）。
 *
 * 判据是**精确零**而不是"低电平"：真实麦克风的底噪几乎不可能长期精确为 0
 * （本机实测环境噪声 raw 39~72）；连续精确 0 说明采集设备交回的是静音缓冲。
 *
 * 单元测试：`SilentInputWatchdogTest`（纯 JVM，无 Android 依赖）。
 */
class SilentInputWatchdog(
    /** 连续零电平达到该帧数即触发（20ms/帧 ⇒ 250 帧 = 5 秒） */
    private val maxZeroFrames: Long,
    /** 触发时的回调（只带连续零帧数） */
    private val onSilent: (Long) -> Unit,
) {
    private var zeroRun = 0L
    private var firedThisRun = false

    /** 喂一帧的原始 RMS；返回 true 表示本帧触发了回调 */
    fun feed(rms: Float): Boolean {
        if (rms != 0f) {
            if (zeroRun > 0) zeroRun = 0
            firedThisRun = false
            return false
        }
        zeroRun++
        if (!firedThisRun && zeroRun >= maxZeroFrames) {
            firedThisRun = true
            onSilent(zeroRun)
            return true
        }
        return false
    }

    /** 供测试/复位用 */
    fun reset() {
        zeroRun = 0
        firedThisRun = false
    }
}
