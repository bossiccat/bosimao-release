// Jax Voice (贾克斯语音) — V1.5 手机语音主线 M1
// 根构建脚本：仅声明插件版本，不装配
plugins {
    id("com.android.application") version "8.6.1" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}

// P0-GRADLE-003：依赖锁定——所有项目、所有配置（含 buildscript classpath）的
// 解析结果固定到各项目目录的 gradle.lockfile，锁文件随仓库提交。
// 效果：Maven 传递依赖版本受锁固定；配合 --offline 实现离线可复现构建。
// 升级依赖时：改版本号后执行 `gradlew dependencies --write-locks`（联网）重新生成。
allprojects {
    dependencyLocking {
        lockAllConfigurations()
    }
    buildscript {
        configurations.classpath {
            resolutionStrategy.activateDependencyLocking()
        }
    }
}
