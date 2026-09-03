# 波斯猫长期阻断评估（2026-08-16）

## 裁决

当前拖延已构成发布治理问题，不是单一 Android 代码问题。必须停止对同一 Windows Gradle 锁错误做等价重试，切换为“环境定界 + 远端 CI + 真机证据”三线并行。整体发布维持 `FAIL`。

## 已确认的正向进展

- `RtcClient` 同步 `enterRoom()` 失败后 attempt 残留已修复。
- 干净 ASCII 构建副本中，完整 `:app:testDebugUnitTest` 已真实 `BUILD SUCCESSFUL`，exit `0`。
- Android CI 已补齐 sherpa-onnx AAR 与 KWS 模型依赖闭集、下载 timeout/retry/heartbeat/hash 检查。

这些结果只证明局部代码和 JVM 单测门，不证明 APK、签名、真机或商业发布可用。

## 长期阻断分类

| 阻断 | 最早可追溯 | 分类 | 当前事实 | 正确处理 |
|---|---:|---|---|---|
| Gradle native/platform 与缓存锁 | 2026-08-14 | Windows 执行环境 | `native-platform.dll.lock`、`fileHashes.lock`、wrapper ZIP lock 均出现 Access denied；已实证新 ASCII user-home 的直接 Gradle 8.7 `--version` 可 exit 0，但 Android build 进程又访问 `C:\Users\Administrator\native\...dll.lock` 并被拒绝 | 停止等价本机重试；定位运行令牌/端点防护对用户级 native 目录的重定向或拒绝，并将 Android 构建主证据迁至 GitHub Windows runner |
| Android 四独立构建门 | 2026-08-14 | 部分已验证、部分环境阻断 | 单测已通过；compile、assembleDebug 近期受 native lock 拦截；assembleRelease 未重新执行 | 在隔离环境和 GitHub Windows runner 上分别取原始日志、退出码和 artifact |
| 远端 GitHub CI 最终证据 | 2026-08-15 | 外部执行证据缺失 | 历史 workflow 曾因缺 sherpa AAR 失败，依赖闭集已修；最终四门日志/artifact 未审计确认 | 触发新的可追溯 run，保存每个 matrix job 的日志、exit 和 APK artifact |
| Release APK 与签名 | 2026-08-14 | 未执行/外部证照 | 没有当前候选可归因 release APK、`apksigner verify --print-certs`、签名 provenance | 等 release assemble 成功后生成固定 SHA-256、签名证据和 provenance；未签名 APK 不可发布 |
| Samsung S26 与 TRTC 双端语音 | 2026-08-15 | 真实设备/工具缺失 | 当前无完整 ADB/scrcpy/平台工具和可审计候选 APK；双轮音频矩阵未跑 | 并行补齐 USB debugging、ADB/platform-tools、可安装 APK，然后跑进房/下行/路由/重启恢复矩阵 |
| Windows 弹窗与 watchdog 取证 | 2026-08-14 | 现场权限/设备会话缺失 | 静态退役逻辑和契约通过，但 Task Scheduler 精确查询曾 `query_error`；无真实交互桌面六场景证据 | 在具备系统工具权限的真实 Windows 交互会话执行 found/not_found/query_error 三态、清理前后与 2x5 分钟观察 |
| Sidecar 原子发布与冷缓存 | 2026-08-14 | 代码/构建环境混合 | 缓存并发原子发布、冷缓存 npm 停滞和 packaged runtime 真实启动仍未闭环 | 独立于 Android 继续执行，不应等待 Gradle 解决 |

## 研究结论：Gradle 锁问题

AGP `8.6.1` + Gradle `8.7` + JDK `17` 是官方匹配组合，不应通过升降版本掩盖问题。

优先级顺序：

1. 关闭 Android Studio/IDE 的 Gradle sync，并检查同版本 Gradle daemon；仅结束已识别的进程。
2. 使用当前用户可写、ASCII、非同步盘且非受保护目录的独立 `GRADLE_USER_HOME`，先运行 `gradlew.bat --version --stacktrace`。这一步失败即证明是 Gradle native 提取目录、ACL、端点防护或路径策略问题，尚未涉及项目代码。
3. 已实证：直接 Gradle 8.7 在新 ASCII user-home 中可 `--version` exit 0；但 Android build 即使显式设置 `GRADLE_USER_HOME`，仍访问 `C:\Users\Administrator\native\...dll.lock` 并被拒绝。该行为把故障定位到执行令牌、native 目录重定向或端点防护，不是项目 `.gradle` cache。
4. 熔断本机等价重试，检查当前 Windows token 的 home/native 目录 ACL、所有者、环境变量继承、Windows Defender/EDR/Controlled Folder Access 审计日志；只修被证实的权限与策略。
5. 将四门主验证迁到 GitHub Windows CI：每 job 使用独立 Gradle user home，构建使用 `--no-daemon`，最后无论成功失败都执行同版本 Wrapper 的 `--stop`。

禁止的绕过：运行时删除或重命名 `.lock`、将 `.dll.lock` 改名为 `.dll`、长期管理员运行 IDE/Gradle、强杀所有 `java.exe`、关闭全局 Defender/EDR、共享 self-hosted runner 的 Gradle User Home。

## 最短收敛路线

### 可立即并行

- 环境线：执行“Wrapper 启动 -> 用户级 cache -> 项目级 cache”三段诊断，确定 Windows ACL/EDR/IDE 并发归因。
- CI 线：为 GitHub Windows matrix 固化隔离 cache、`--no-daemon`、`always()` daemon stop，并重新触发四独立门。
- Windows 线：恢复现场系统工具权限后，完成 watchdog 三态和无窗口动态取证。
- Sidecar 线：继续原子发布、冷缓存和 packaged runtime 验证。
- 真机准备线：安装/定位 platform-tools，确认 Samsung S26 USB debugging 与电脑控制链路，不等待 Gradle 完全修复。

### 必须串行

1. Android `assembleRelease` 成功。
2. 对该 APK 计算 SHA-256，执行签名验证，生成 provenance。
3. 对同一 SHA 的 APK 进行 Samsung S26 安装和双轮 TRTC 音频测试。
4. 只有 Android、Windows、sidecar、签名和真机 P0 Claim 全部具备原始证据时，才允许重新评估 Overall Release。

## 用户需要协助的最小事项

- 不需要你盲目重试构建或当测试员。
- 当进入真机验证时，需要在 Samsung S26 开启开发者选项和 USB debugging，并确认电脑 USB 调试授权。
- 当 Windows 现场取证恢复时，需要保证一次真实交互桌面会话和 Task Scheduler 查询权限可用。
- 代码签名仍需可用证书/签名服务；未具备前，release build 可以验证但不能发布。

## 当前发布状态

```text
RTC 同步失败修复：PASS
完整 JVM 单测：PASS
Android compile/assemble：BLOCKED（Windows 执行令牌/端点防护对用户级 Gradle native 目录拒绝访问）
Android release APK/签名：NOT_RUN
GitHub 四门最终证据：UNVERIFIED
Samsung S26/TRTC 双端验证：NOT_RUN
Windows 无窗口现场验证：EVIDENCE_PENDING
Sidecar packaged runtime：EVIDENCE_PENDING
Overall Release：FAIL
```

## 参考

- Android Gradle Plugin 8.6.0 发布说明：https://developer.android.google.cn/build/releases/past-releases/agp-8-6-0-release-notes?hl=zh-cn
- Gradle 管理目录与缓存：https://docs.gradle.org/userguide/directory_layout.html
- Gradle Daemon 故障排查：https://docs.gradle.org/current/userguide/gradle_daemon.html
- Gradle CLI 与 project cache：https://docs.gradle.org/current/userguide/command_line_interface.html
- Gradle Windows native 路径问题：https://github.com/gradle/gradle/issues/30451
- Gradle project cache lock 权限问题：https://github.com/gradle/gradle/issues/6121
- GitHub Actions Gradle cache 锁案例：https://github.com/actions/setup-java/issues/633
