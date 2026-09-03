# 波斯猫商业化 P0 Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 在不绕过任何既有 P0 的前提下，解耦 GitHub PR 门与发布门，并完成 Windows、Android、packaged sidecar 的真实商业发布证据闭环。

**Architecture:** 工作按治理、Windows、Android、sidecar 四个独立风险域并行调查，任何共享文件修改均在专用 worktree 串行实施。每个风险域严格分为代码实现、机器回归、真实现场、独立 Claim 审核；最终发布总门只消费已验证 Claim、受控 `v*` 运行和远端强制治理证据。

**Tech Stack:** GitHub Actions、Python 3.11/3.13、pytest 9.1.1、Node/Electron、Rust/Tauri、PowerShell/Win32、Android/Kotlin/Gradle、TRTC。

---

## 不可变门禁

1. `windows-popup-free` 和 `android-duplex-audio` 在真实现场证据闭环前保持 `EvidencePending`。
2. PR 门可验证 candidate/checksum/provenance/regression/Claim 结构，但不得访问生产密钥或执行 release。
3. `v*` 发布门必须执行完整 Claim verify + HMAC locked checks + manifest；任一 P0 非 `Verified` 即失败。
4. 静态扫描、进程存活、`/health`、mock、模拟走查、同模型自审不能代替现场证据。
5. 任何实现任务都必须经过规格审查、质量审查和完整回归；任何 Claim 升级必须 owner/reviewer 分离。
6. 主工作区现有 Android/sidecar/watchdog 未提交改动不得被覆盖、回退或整体提交。

## Phase A：GitHub CI 双门治理

### Task A1：锁定 PR/release 双门契约

**Owner:** Architect  
**Files:**
- Read: `.github/workflows/release-governance.yml`
- Read: `scripts/release-preflight.py`
- Read: `scripts/release_governance/*.py`
- Read: `governance/release-policy.json`
- Read: `scripts/test_release_governance/*.py`
- Read: `docs/governance/release-harness.md`

**Steps:**
1. 读取真实 run `31897068571` 的 job/step/失败日志。
2. 定义 PR gate 与 `v*` release gate 的输入、输出、权限和失败语义。
3. 判断是否新增 preflight 子命令；禁止在 PR 使用完整发布 Claim 状态作为 required check。
4. 用 EARS 写验收标准和稳定 status 名称。
5. 回传 RoleVerdict；不得修改文件。

### Task A2：先写 CI 反作弊 RED 测试

**Owner:** QA  
**Test:** `scripts/test_release_governance/test_ci_policy.py`

**RED assertions:**
- PR 必须运行 build/checksum/provenance/regression/PR gate。
- PR 不得访问 production environment/HMAC 或执行 release。
- `v*` 必须完整执行 Claim verify/release。
- 禁止 `continue-on-error`、对 Claim 步骤的 `if: always()`、错误的 skipped-as-success。
- EvidencePending 在 tag run 必须阻断。

Run:
```bash
python -m pytest scripts/test_release_governance/test_ci_policy.py -q --basetemp <fresh-dir>
```
Expected: 新增断言因现有 PR 调用完整 Claim verify 而 FAIL。

### Task A3：实施最小 CI 解耦

**Owner:** Backend  
**Modify only as contract requires:**
- `.github/workflows/release-governance.yml`
- `scripts/release-preflight.py` 或精确 helper
- `scripts/test_release_governance/test_ci_policy.py`
- 相关行为测试
- `docs/governance/release-harness.md`

**Steps:**
1. 在隔离 worktree 实施最小改动。
2. 运行 focused 测试并取得 GREEN。
3. 运行完整 `scripts/test_release_governance`。
4. 解析 workflow YAML，执行 `git diff --check`。
5. 只提交治理文件，禁止带入主工作区改动。

### Task A4：两阶段独立审查

1. Architect 逐条核对规格，不通过则返回 A3 修复。
2. QA 审查反作弊、密钥范围、条件表达式、测试删除/skip/focus，不通过则返回 A3。
3. 只有两份 RoleVerdict 都为 pass 才进入远端 PR。

### Task A5：真实 PR run

**Owner:** DevOps

1. 推送隔离分支并创建/更新 PR。
2. 等待真实 Actions 完成。
3. 必须看到 build/checksum/provenance/test/PR gate SUCCESS，release SKIPPED。
4. 读取 job 日志，确认 PR 未获得 HMAC secret。
5. 保存 PR URL、run URL、head SHA 和结论。

### Task A6：受控 `v*` 负向发布验证

1. QA 定义唯一测试 tag 与预期失败点。
2. DevOps 创建 tag 并触发真实 workflow。
3. candidate/checksum/provenance/test 必须成功。
4. Claim gate 必须因两个 `EvidencePending` 失败。
5. 正式发布步骤和有效 manifest 必须不存在。
6. 不得伪造 Verified Claim 获取绿灯。

## Phase B：Windows 无命令窗口 P0

### Task B1：只读审计与施工矩阵

**Owner:** Frontend/Windows

**Read:**
- `scripts/install-scheduled-tasks.ps1`
- `scripts/watchdog-check.ps1`
- `scripts/watchdog-sidecar.ps1`
- `scripts/windows-window-lineage-*`
- `scripts/test/legacy-*watchdog*`
- `windows-popup-field-evidence.ps1`
- `pet-ui/src-tauri` launcher/supervisor files

**Output:** 旧 scheduled tasks、旧进程、启动入口、packaged 六场景的依赖 DAG；标注当前主机、管理员、交互桌面、clean VM 资源要求。

### Task B2：TDD 修复 legacy watchdog/launcher

1. 在专用 worktree 写失败测试。
2. 验证失败原因是旧任务/console host/错误 launcher 契约。
3. 实施最小修复，不覆盖主工作区并行改动。
4. focused/full tests GREEN。
5. 规格审查 + 质量审查后提交。

### Task B3：真实现场清场

1. 精确列出 scheduled task、旧进程和启动入口。
2. 记录清场前证据。
3. 仅处理已确认的精确对象。
4. 清场后复查；无输出不能解释为不存在。

### Task B4：packaged 六场景动态验收

场景：首次启动、正常重启、异常退出、App 被杀、登录恢复、卸载后。

每个场景记录：
- 实际进程名与完整命令行；
- 可见窗口/console host/window lineage；
- packaged sidecar 启动与就绪；
- `/health` 仅作辅助；
- 退出/恢复结果。

### Task B5：独立 Claim 裁决

只有当前 commit、Windows 工件 SHA-256、六场景证据、未过期时间和独立 reviewer 全部有效时，才允许 `windows-popup-free=Verified`。

## Phase C：Android/TRTC P0

### Task C1：只读审计依赖 DAG

**Read:**
- `mobile-app/.../RtcClient.kt`
- `VoiceForegroundService.kt`
- `VoiceSessionCoordinator.kt`
- 对应单测、Gradle release/signing 配置

**Output:** 状态机、cleanup、remote audio、签名 APK、双轮真机和 App kill 恢复的依赖关系与测试矩阵。

### Task C2：TDD 修复 TRTC 状态机

状态锁定：`IDLE -> SIGNING -> ENTERING -> IN_ROOM -> EXITING -> IDLE`。

覆盖取消、超时、网络断开、重复回调、App kill/restart 和可重入 cleanup。

### Task C3：TDD 修复下行音频

覆盖 remote-user 订阅、播放、扬声器/耳机/蓝牙路由、资源释放和重连。mock 只用于代码回归，不作为真机通过证据。

### Task C4：构建可归因签名 APK

记录 commit、Gradle 输入、版本号、证书指纹和 APK SHA-256；密钥不得进入日志、仓库或对话。

### Task C5：连续双轮真机语音

依赖 Windows Claim、签名 APK、packaged sidecar 真启动。必须覆盖：
- 两轮连续全双工；
- barge-in 打断；
- Android 下行播放；
- 网络切换；
- App 被杀后恢复。

### Task C6：独立 Claim 裁决

只有当前工件与连续双轮现场证据绑定且 owner/reviewer 分离时，才允许 `android-duplex-audio=Verified`。

## Phase D：Packaged Sidecar P0

### Task D1：只读审计缓存与启动链

**Read:**
- `scripts/build-sidecar-external-bin.js`
- `scripts/create-sidecar-runtime-fixture.js`
- `scripts/lib/sidecar-package-build.js`
- sidecar package/launch tests
- `pet-ui/src-tauri/build.rs`, `sidecar.rs`, `sidecar_integrity.rs`, `tauri.conf.json`

输出缓存锁、并发发布、cold-cache、安装布局、runtime trust、TRTC readiness/heartbeat 的 DAG 与资源冲突矩阵。

### Task D2：修复缓存并发与原子发布

按 TDD 覆盖并发去重、临时目录、hash 验证、原子提交、失败残留隔离和重复运行。

### Task D3：诊断 cold-cache npm

用全新缓存目录重现，采集网络/代理/锁/子进程/超时证据。只修根因，禁止只加 timeout 或 warm-cache 假绿。

### Task D4：packaged 真启动验收

从安装路径验证品牌化进程、binary/runtime hash、runtime trust、TRTC readiness/heartbeat、异常退出和宿主重启恢复。`/health` 单独成功不能通过。

## Phase E：最终商业发布总门

必须同时满足：

- CI PR 双门实现及真实 PR run 通过；
- 受控 `v*` 负向验证按预期失败且无正式发布；
- `windows-popup-free=Verified`；
- `android-duplex-audio=Verified`；
- packaged sidecar 真实启动与就绪通过；
- 远端 protected branch/required status/CODEOWNERS review/tag restriction 生效；
- 独立 reviewer 与签名资源就绪。

任一项缺失：Overall Release=`FAIL`。

## 外部未决项

- O-021：GitHub 私有仓库强制治理套餐。
- O-022：独立发布 reviewer 身份。
- O-023：代码签名与真实设备资源。

这些项只能由远端设置、真实审批、签名工件和现场证据关闭，不能由本地代码或任务状态关闭。
