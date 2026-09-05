package com.jax.voice.net

/**
 * 自定义采集 PCM 工具（2026-09-05 回音/误打断根治）：
 * 契约 = 16k / mono / PCM16LE / 20ms 帧 = 640 字节（SPEC §4.1 / AC-08，与 bridge 侧 hello audio_format 一致）。
 * 纯 JVM 可测；不做重采样（采集源即 16k，AudioSource.MIC）。
 */
object RtcCustomAudioPcm {

    const val SAMPLE_RATE = 16000
    const val FRAME_MS = 20
    const val SAMPLES_PER_20MS = SAMPLE_RATE * FRAME_MS / 1000 // 320
    const val FRAME_BYTES_20MS = SAMPLES_PER_20MS * 2 // 640

    /** ShortArray → PCM16LE 小端字节（TRTCAudioFrame.data 契约） */
    fun shortToPcm16le(samples: ShortArray): ByteArray {
        val out = ByteArray(samples.size * 2)
        for (i in samples.indices) {
            val v = samples[i].toInt()
            out[i * 2] = (v and 0xFF).toByte()
            out[i * 2 + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return out
    }

    /**
     * 采集帧（任意长度，通常 40ms/640 samples 与 MicRecorder 契约对齐）→ 20ms 整帧列表。
     * 尾部不足 320 样本时补零到整帧（保持实时发送节奏，不让残帧积压）。
     */
    fun splitInto20msFrames(samples: ShortArray, sampleRate: Int = SAMPLE_RATE): List<ByteArray> {
        require(sampleRate == SAMPLE_RATE) { "仅支持 16k（契约）；实际 $sampleRate" }
        val out = mutableListOf<ByteArray>()
        var i = 0
        while (i < samples.size) {
            val n = minOf(SAMPLES_PER_20MS, samples.size - i)
            val buf = ShortArray(SAMPLES_PER_20MS)
            System.arraycopy(samples, i, buf, 0, n)
            out.add(shortToPcm16le(buf))
            i += n
        }
        return out
    }
}
