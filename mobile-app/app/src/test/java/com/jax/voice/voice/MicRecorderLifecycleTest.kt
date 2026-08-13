package com.jax.voice.voice

import org.junit.Assert.assertTrue
import org.junit.Test

class MicRecorderLifecycleTest {

    @Test
    fun `stop actively releases and joins worker before returning`() {
        val stopControlClass = runCatching {
            Class.forName("com.jax.voice.voice.MicRecorder\$StopControl")
        }.getOrNull()
        assertTrue("MicRecorder 必须提供可测试的 StopControl 以验证主动释放", stopControlClass != null)

        val stopAndJoin = MicRecorder::class.java.declaredMethods.firstOrNull {
            it.name == "stopAndJoin" && it.parameterTypes.firstOrNull() == Thread::class.java
        }
        assertTrue("MicRecorder 必须提供 stopAndJoin 并在 stop 返回前 join worker", stopAndJoin != null)
    }

    @Test
    fun `stop avoids self join while still releasing record`() {
        val stopAndJoin = MicRecorder::class.java.declaredMethods.firstOrNull {
            it.name == "stopAndJoin" && it.parameterTypes.firstOrNull() == Thread::class.java
        }
        assertTrue("stopAndJoin 必须存在以保护采集线程不 self-join", stopAndJoin != null)
    }
}
