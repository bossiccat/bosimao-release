plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.jax.voice"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.jax.voice"
        minSdk = 26
        targetSdk = 35
        versionCode = 13
        versionName = "0.6.7"

        // TRTC 官方要求指定 CPU 架构（缩包体；ADR-012 R2 版本锁定）
        ndk {
            abiFilters += listOf("armeabi-v7a", "arm64-v8a")
        }

        // M0 采集源 A/B（decision-relocation §M0）：gradle -PjaxCaptureSource=VOICE_COMMUNICATION
        // 注入实验变体；默认无 -P 时为 MIC，与生产行为完全一致。
        resValue("string", "jax_capture_source",
            (project.findProperty("jaxCaptureSource") as String? ?: "MIC").trim().uppercase())

        // 采集音效 A/B（2026-09-16 真机排查 S26U/Android16 "说话无反应"）：
        // gradle -PjaxCaptureEffects=NONE 注入「不挂平台 AEC/NS/AGC」的对照包，
        // 用于判定"采集恒零"是否由音效挂载造成。默认 AEC_NS = 生产行为**完全不变**。
        // 背景：代码注释本机有前科（CaptureGainStage.kt:48「VOICE_COMMUNICATION 源送全零，是死路」），
        // 且"有声"的 MicRecorder 路径并不挂音效，而"读 0"的这条挂了。
        resValue("string", "jax_capture_effects",
            (project.findProperty("jaxCaptureEffects") as String? ?: "AEC_NS").trim().uppercase())

        // 采集音频模式 A/B（2026-09-16，根因：本机在 AUDIOCALL 通话形态下向自采返回全零）：
        // -PjaxCaptureMode=NORMAL ⇒ 建自采 AudioRecord 那一刻把 AudioManager.mode 置 MODE_NORMAL，
        // 建完恢复。默认 KEEP = 生产行为完全不变。
        resValue("string", "jax_capture_mode",
            (project.findProperty("jaxCaptureMode") as String? ?: "KEEP").trim().uppercase())
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }

    // JVM 单测（app/src/test/）：android.* stub 方法返回默认值，避免 android.util.Log 抛 not-mocked
    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    // sherpa-onnx Android AAR（脚本 scripts/fetch-deps.ps1 下载到 app/libs/，见 README）
    implementation(fileTree(mapOf("dir" to "libs", "include" to listOf("*.jar", "*.aar"))))

    // TRTC 精简版 SDK（纯音频通话 + 直播播放；ADR-012 锁精确版本，禁止 latest.release）
    // 13.4 稳定线最新精确版（2026-06 发布）；升级必须走回归门禁
    implementation("com.tencent.liteav:LiteAVSDK_TRTC:13.4.0.20477")

    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.activity:activity-ktx:1.9.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0") // REST client（会话签发接口）

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    // RtcClient 状态机 L0 单测（RTC-CLIENT-TEST-DESIGN §2）：mock TRTCCloud，不连真实 RTC 云
    testImplementation("io.mockk:mockk:1.13.5")
}
