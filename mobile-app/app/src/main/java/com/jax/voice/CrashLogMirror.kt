package com.jax.voice

interface CrashLogMirrorStore {
    fun replace(name: String, content: String)
    fun delete(name: String)
}

class CrashLogMirror(private val store: CrashLogMirrorStore) {
    fun write(content: String) {
        store.replace(JaxApp.CRASH_LOG_NAME, content)
    }

    fun clear() {
        store.delete(JaxApp.CRASH_LOG_NAME)
    }
}
