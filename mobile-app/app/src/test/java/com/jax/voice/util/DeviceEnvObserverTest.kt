package com.jax.voice.util

import android.media.AudioDeviceInfo
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DeviceEnvObserverTest {

    @Test
    fun `deviceTypeLabel maps known device types to human readable names`() {
        assertEquals(
            "bluetooth_a2dp",
            DeviceEnvObserver.deviceTypeLabel(AudioDeviceInfo.TYPE_BLUETOOTH_A2DP)
        )
        assertEquals(
            "bluetooth_sco",
            DeviceEnvObserver.deviceTypeLabel(AudioDeviceInfo.TYPE_BLUETOOTH_SCO)
        )
        assertEquals(
            "wired_headset",
            DeviceEnvObserver.deviceTypeLabel(AudioDeviceInfo.TYPE_WIRED_HEADSET)
        )
        assertEquals(
            "wired_headphones",
            DeviceEnvObserver.deviceTypeLabel(AudioDeviceInfo.TYPE_WIRED_HEADPHONES)
        )
        assertEquals(
            "usb_headset",
            DeviceEnvObserver.deviceTypeLabel(AudioDeviceInfo.TYPE_USB_HEADSET)
        )
        assertEquals(
            "builtin_speaker",
            DeviceEnvObserver.deviceTypeLabel(AudioDeviceInfo.TYPE_BUILTIN_SPEAKER)
        )
    }

    @Test
    fun `deviceTypeLabel falls back to type_N for unknown device types`() {
        assertEquals("type_12345", DeviceEnvObserver.deviceTypeLabel(12345))
    }

    @Test
    fun `transportLabelFromFlags labels wifi cellular and combinations`() {
        assertEquals("wifi", DeviceEnvObserver.transportLabelFromFlags(hasWifi = true, hasCellular = false))
        assertEquals("cellular", DeviceEnvObserver.transportLabelFromFlags(hasWifi = false, hasCellular = true))
        assertEquals(
            "wifi+cellular",
            DeviceEnvObserver.transportLabelFromFlags(hasWifi = true, hasCellular = true)
        )
        assertEquals("other", DeviceEnvObserver.transportLabelFromFlags(hasWifi = false, hasCellular = false))
    }

    @Test
    fun `dedup suppresses consecutive identical events within a category but not across categories`() {
        val dedup = DeviceEnvObserver.Dedup()
        // 同类别首次出现应记录
        assertTrue(dedup.shouldEmit("network", "wifi up"))
        // 同类别完全相同应被去重
        assertFalse(dedup.shouldEmit("network", "wifi up"))
        // 同类别不同文本应记录
        assertTrue(dedup.shouldEmit("network", "cellular up"))
        // 不同类别相同文本不应被跨类别去重
        assertTrue(dedup.shouldEmit("screen", "wifi up"))
    }

    @Test
    fun `dedup allows re-emitting a text after the category text changes back`() {
        val dedup = DeviceEnvObserver.Dedup()
        assertTrue(dedup.shouldEmit("network", "wifi up"))
        assertTrue(dedup.shouldEmit("network", "cellular up"))
        // 切走再切回，应再次允许（避免真实网络来回抖动时永久沉默）
        assertTrue(dedup.shouldEmit("network", "wifi up"))
    }

    @Test
    fun `buildEventText composes prefix and detail`() {
        assertEquals(
            "audio_device: added type=bluetooth_a2dp id=7",
            DeviceEnvObserver.buildEventText("audio_device", "added type=bluetooth_a2dp id=7")
        )
    }
}
