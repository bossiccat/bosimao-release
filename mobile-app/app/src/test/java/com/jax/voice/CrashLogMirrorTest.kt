package com.jax.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class CrashLogMirrorTest {

    @Test
    fun `repeated crash writes update one stable mirror record`() {
        val store = RecordingCrashLogMirrorStore()
        val mirror = newMirror(store)

        invokeMirror(mirror, "write", "first")
        invokeMirror(mirror, "write", "second")

        assertEquals(1, store.records.size)
        assertEquals("second", store.records[JaxApp.CRASH_LOG_NAME])
    }

    @Test
    fun `clear removes the stable mirror record`() {
        val store = RecordingCrashLogMirrorStore()
        val mirror = newMirror(store)
        invokeMirror(mirror, "write", "crash")

        invokeMirror(mirror, "clear")

        assertNull(store.records[JaxApp.CRASH_LOG_NAME])
    }

    private fun newMirror(store: RecordingCrashLogMirrorStore): Any {
        val storeClass = runCatching { Class.forName("com.jax.voice.CrashLogMirrorStore") }.getOrNull()
        val mirrorClass = runCatching { Class.forName("com.jax.voice.CrashLogMirror") }.getOrNull()
        org.junit.Assert.assertNotNull("必须抽象 CrashLogMirrorStore 以测试 MediaStore 稳定记录替换", storeClass)
        org.junit.Assert.assertNotNull("必须提供 CrashLogMirror 以避免重复崩溃产生多份下载记录", mirrorClass)
        val proxy = java.lang.reflect.Proxy.newProxyInstance(
            storeClass!!.classLoader,
            arrayOf(storeClass)
        ) { _, method, args ->
            when (method.name) {
                "replace" -> store.records[args!![0] as String] = args[1] as String
                "delete" -> store.records.remove(args!![0] as String)
            }
            null
        }
        return mirrorClass!!.getConstructor(storeClass).newInstance(proxy)
    }

    private fun invokeMirror(mirror: Any, name: String, vararg args: String) {
        val types = Array(args.size) { String::class.java }
        mirror.javaClass.getMethod(name, *types).invoke(mirror, *args)
    }

    private class RecordingCrashLogMirrorStore {
        val records = mutableMapOf<String, String>()
    }
}
