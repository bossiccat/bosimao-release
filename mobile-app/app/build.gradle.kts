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
        versionCode = 6
        versionName = "0.6.0"

        // TRTC 官方要求指定 CPU 架构（缩包体；ADR-012 R2 版本锁定）
        ndk {
            abiFilters += listOf("armeabi-v7a", "arm64-v8a")
        }
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
    // RtcClient 状态机 L0 单测（RTC-CLIENT-TEST-DESIGN §2）：mock TRTCCloud，不连真实 RTC 云
    testImplementation("io.mockk:mockk:1.13.5")
}
