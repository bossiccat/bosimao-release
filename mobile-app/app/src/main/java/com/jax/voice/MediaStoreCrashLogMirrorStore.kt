package com.jax.voice

import android.content.ContentResolver
import android.content.ContentValues
import android.os.Build
import android.os.Environment
import android.provider.MediaStore

class MediaStoreCrashLogMirrorStore(
    private val resolver: ContentResolver
) : CrashLogMirrorStore {
    override fun replace(name: String, content: String) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        delete(name)
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, name)
            put(MediaStore.MediaColumns.MIME_TYPE, "text/plain")
            put(MediaStore.MediaColumns.RELATIVE_PATH, relativePath())
        }
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) ?: return
        resolver.openOutputStream(uri, "w")?.use { output ->
            output.write(content.toByteArray(Charsets.UTF_8))
        }
    }

    override fun delete(name: String) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return
        val selection = "${MediaStore.MediaColumns.DISPLAY_NAME}=? AND ${MediaStore.MediaColumns.RELATIVE_PATH}=?"
        resolver.delete(
            MediaStore.Downloads.EXTERNAL_CONTENT_URI,
            selection,
            arrayOf(name, relativePath())
        )
    }

    private fun relativePath(): String = Environment.DIRECTORY_DOWNLOADS + "/波斯猫/"
}
