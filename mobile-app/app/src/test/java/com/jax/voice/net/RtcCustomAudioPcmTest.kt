package com.jax.voice.net

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 自定义采集 PCM 工具 L0 单测（16k/mono/PCM16LE/20ms=640B 契约，SPEC §4.1/AC-08）。
 */
class RtcCustomAudioPcmTest {

    @Test
    fun shortToPcm16le_roundTrip_littleEndian() {
        val samples = shortArrayOf(0, 1, -1, 32767, -32768, 97)
        val bytes = RtcCustomAudioPcm.shortToPcm16le(samples)
        assertEquals(samples.size * 2, bytes.size)
        // 小端：低字节在前（Byte 必须显式 toInt 比较，避免装箱不等）
        assertEquals(0, bytes[0].toInt()); assertEquals(0, bytes[1].toInt())
        assertEquals(1, bytes[2].toInt()); assertEquals(0, bytes[3].toInt())
        assertEquals(0xFF, bytes[4].toInt() and 0xFF); assertEquals(0xFF, bytes[5].toInt() and 0xFF) // -1
        assertEquals(0xFF, bytes[6].toInt() and 0xFF); assertEquals(0x7F, bytes[7].toInt() and 0xFF) // 32767
        assertEquals(0x00, bytes[8].toInt() and 0xFF); assertEquals(0x80, bytes[9].toInt() and 0xFF) // -32768
        assertEquals(97, bytes[10].toInt() and 0xFF); assertEquals(0, bytes[11].toInt() and 0xFF)
    }

    @Test
    fun splitInto20msFrames_640samples_yieldsTwo640ByteFrames() {
        // 40ms 采集帧（640 samples @16k，MicRecorder 契约）→ 两个 20ms 帧
        val samples = ShortArray(640) { (it % 327).toShort() }
        val frames = RtcCustomAudioPcm.splitInto20msFrames(samples, sampleRate = 16000)
        assertEquals(2, frames.size)
        assertEquals(640, frames[0].size)
        assertEquals(640, frames[1].size)
        // 内容连续性：第二帧首样本 = samples[320]
        val secondFirst = (frames[1][0].toInt() and 0xFF) or ((frames[1][1].toInt() and 0xFF) shl 8)
        assertEquals(samples[320].toInt(), secondFirst.toShort().toInt())
    }

    @Test
    fun splitInto20msFrames_partialTail_paddedToFullFrame() {
        val samples = ShortArray(641) { 5 } // 40ms + 1 样本
        val frames = RtcCustomAudioPcm.splitInto20msFrames(samples, sampleRate = 16000)
        assertEquals(3, frames.size)
        assertEquals(640, frames[2].size) // 尾帧补零到 20ms 整帧
        assertEquals(0, frames[2][2].toInt()); assertEquals(0, frames[2][3].toInt()) // 补零
        assertEquals(5, frames[2][0].toInt() and 0xFF) // 唯一剩余样本在首位
    }
}
