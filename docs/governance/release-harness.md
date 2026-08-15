# Release Governance Harness

## 目的与裁决边界

本 Harness 是官方发布入口的 fail-closed 控制面。它校验 P0 Claim、现场证据、当前 Git commit、待发布工件精确字节、锁定检查结果和发布 manifest。只有受保护 CI 在完成这些检查后，才可继续执行官方发布步骤。

它不能阻止：

- 本机直接复制 EXE/APK 或手工运行脚本；
- WorkBuddy TaskUpdate / TaskComplete 状态漂移；
- 未接入受保护 CI 的本地命令被用户自行调用。

因此本地 `verify` / `release` 只代表仓库内控制面结果。未配置远端 protected branch、required status、production environment reviewer 和 CI-only 发布凭据时，治理等级只能是 `LOCAL_ONLY`。

## GitHub 托管前置与当前等级

仓库中的 `.github/workflows/release-governance.yml` 和 `.github/CODEOWNERS` 只声明 CI 契约，不能自行创建或验证 GitHub 的远端保护设置。远端私有仓库 `bossiccat/bosimao-release` 已建立，默认分支为 `master`；GitHub Free 私有组织仓库当前不提供本项目所需的完整 Rulesets 强制能力。因此当前等级仍为 `LOCAL_ONLY`，不得声称生产审批或不可绕过的分支保护已经启用。

仓库管理员必须在 GitHub 托管端确认并启用以下控制：保护 default branch；限制 `v*` tag 创建；要求 CODEOWNERS 审核；将 `release-governance` 设为 required status；为 `production` environment 配置 reviewer；只将发布 token 和 `RELEASE_EVIDENCE_HMAC_KEY` 授予该 CI environment。当前 `production` environment 已创建且部署来源限制为 `v*` tag，但当前套餐未提供 required reviewer。完成套餐升级或等价外部门禁后，还必须以实际 PR 和 `v*` tag run 复核这些规则；在该复核留下证据前，Harness 不可标记为生产就绪。

`.github/CODEOWNERS` 已绑定经 GitHub 组织成员 API 确认为 `bossiccat` 管理员的 `@jinhong1688`，不再引用不存在的占位团队。单一管理员 owner 只能确保 GitHub 能解析 owner 身份，不能形成 owner 与 reviewer 分离，也不能替代 `Require review from Code Owners` 远端规则；在独立 reviewer 和远端保护生效以前，CODEOWNERS 文件本身不提供不可绕过的审批保证。

## CI 工件与 PR 边界

`build-candidate` job 由当前 `$GITHUB_SHA` 生成唯一的 `release-candidate.tar.gz`、同目录 SHA-256 sidecar，以及受版本控制 helper 生成的 `release-candidate.provenance.json`。provenance 固定使用 `release-governance/candidate-provenance/v1` schema，并绑定 build job 当前 HEAD 的完整 commit 与候选工件精确字节的 SHA-256。

`verify` 与 tag-only `release` 都显式 checkout `github.sha`，只下载这三个 artifact 文件，先执行 `sha256sum --check`，再调用 `scripts/release_governance/verify_candidate_provenance.py verify`，最后才调用各自的 preflight。helper 会重新读取当前 checkout 的 `git rev-parse HEAD`，要求 provenance `git_commit`、workflow expected commit、当前 HEAD 三者相等，并重算候选工件 SHA-256 后要求其匹配 provenance。工件下载位置位于 runner 临时目录，避免污染仓库工作区并绕过 fail-closed clean-worktree 检查。

artifact store 的 checksum 只证明下载的字节与上传的 checksum sidecar 一致，即传输完整性；它不单独证明该工件属于本次 workflow 所 checkout 的源码。provenance 绑定只用于阻断候选工件、commit 和 runner HEAD 之间的身份错配；它不替代远端 protected branch、受限 tag 创建、required status、production reviewer、CI-only 凭据或 artifact store 的访问控制。

PR 仍运行无密钥回归测试：`test` job 覆盖 release-governance 的静态与行为回归，`verify` job 对同一候选工件执行 Claim 检查；两者均不接收 production environment 或 `RELEASE_EVIDENCE_HMAC_KEY`。PR 不会调度 `release` job，因而不会创建官方发布或访问发布环境。

当前 release job 只完成 preflight 与证据上传；它不会执行官方发布命令。官方发布命令与最小权限发布凭据未接入，因此凭据锁定目标未完成。此状态不代表生产就绪，也不能作为产品已发布或生产审批已生效的证据。

## 输入可信边界

`release-preflight.py release` 不接受调用方提供的 artifact SHA、旧 manifest 或手工 PASS。它在当前进程中读取 `--artifact-path` 的精确字节并现场计算 SHA-256，并在以下三个时点重新验证：

1. Claim 验证前；
2. locked checks 执行后；
3. manifest 提交前。

每个时点都必须证明：

- `git status --porcelain` 为空；
- HEAD 是可解析的完整 commit；
- artifact SHA 与首次快照一致。

任何无法证明 clean、HEAD 变化或 artifact 变化均为 `RELEASE_INPUT_CHANGED` / `DIRTY_WORKTREE`，不得生成 manifest。每一个 locked check 结束后立即再次快照；一旦 check 污染 tracked/untracked 工作区、移动 HEAD 或修改 artifact，后续 check 不得继续执行，即使后续命令本可尝试还原。

## Claim 与现场证据

每个 required P0 Claim 必须是 `Verified`，绑定当前 commit 和现场计算的 artifact SHA，并具有未过期、可校验、由独立 reviewer 复核的 evidence。当前项目的 Windows 与 Android Claim 仍为 `EvidencePending`；在取得真实 Windows 交互桌面、Android Emulator/真机、packaged sidecar 和连续双端语音证据前，官方发布必须保持 FAIL。

静态测试、health endpoint、进程存活、sidecar smoke 和单独的 `/health` 不能升级为 Windows field 或 Android field evidence。

## Locked check 结果

Policy 的 `required_checks` 是唯一执行顺序。每个 check 必须存在于 `command-lock.json`，不能重复，必须符合安全 ID 格式，不能接受调用方追加参数。runner 使用 `shell=False`，并把结果写入：

```text
artifacts/release-evidence/<release_id>/ci-command/<check_id>/result.json
```

结果包含 schema、release/check ID、Git commit、argv、UTC、退出码、stdout/stderr SHA-256、环境指纹和 collector。受保护 CI 必须通过 `RELEASE_EVIDENCE_HMAC_KEY` 环境注入密钥；runner 读取该密钥仅用于在父进程签名，执行 locked check 时会从子进程环境剥离，受检命令不得读取或伪造 result HMAC。runner 同时写入 `result.hmac`，它是 HMAC-SHA256(canonical result bytes) 的 sidecar；preflight 在读取结果时必须验证 sidecar、schema、release/check ID、commit、status=passed 和 expected exit。缺少密钥、sidecar、字段或摘要不匹配均拒绝。

结果目录和 result 文件采用首次创建语义。同一 release/check 不允许覆盖。check 非零、超时或启动错误会先留存失败结果，然后停止后续 check，不能生成 manifest。

## Manifest 提交与崩溃语义

release ID 在所有 Claim 通过、执行 check 前以 `mkdir(exist_ok=False)` 原子预留。已存在目录、结果、manifest 或 staging 残留时拒绝重用；残留不代表通过。

manifest 先在同目录 staging 文件中完整写入并 fsync，再执行不覆盖提交：

- Windows：使用同卷 `MoveFileExW`，不使用 `MOVEFILE_REPLACE_EXISTING`，并使用 `MOVEFILE_WRITE_THROUGH`；
- POSIX：使用不覆盖 hard-link 提交。

随后对 manifest 文件和父目录执行持久化刷新。Windows 不提供可移植的目录 fsync，因此以 `MoveFileExW(MOVEFILE_WRITE_THROUGH)` 作为文件提交耐久性屏障；任一原生调用失败即 fail-closed。staging 文件可能在成功提交后保留，属于可审计残留；由于 release ID 已占用，残留不能被重试覆盖或当作有效 manifest。

manifest 至少绑定：

- policy SHA-256；
- 发布 commit；
- CI 现场计算的 artifact SHA-256；
- 所有 Claim evidence digest；
- 每个 required check 的 result digest；
- UTC 创建时间；
- CI run URL。

## 运行命令

```bash
python scripts/release-preflight.py verify \
  --policy governance/release-policy.json \
  --claims governance/claims \
  --command-lock governance/command-lock.json \
  --repo-root . \
  --artifact-path <CI待发布工件> \
  --evidence-root artifacts/release-evidence

RELEASE_EVIDENCE_HMAC_KEY=<CI环境密钥> python scripts/release-preflight.py release \
  --release-id <CI不可预测ID> \
  --policy governance/release-policy.json \
  --claims governance/claims \
  --command-lock governance/command-lock.json \
  --repo-root . \
  --artifact-path <CI待发布工件> \
  --evidence-root artifacts/release-evidence \
  --ci-run-url <CI运行URL>
```

发布凭据不得进入本机 shell 历史、仓库、result、manifest、截图或普通日志。正式发布前必须在受保护 CI 配置密钥和人工 reviewer。

## 失败熔断与真实环境前置

同一 `root_cause_key + verification_method + target` 连续两次失败，或 24 小时没有新增有效证据，必须转 `CircuitOpen` / `Escalated`，不得重复重试同一假设。

当前仍未由仓库内部完成的 P0 包括：

- Windows 历史 watchdog 现场清理与 packaged 六场景无命令窗口；
- clean VM、第二用户、升级、回滚、卸载和残留验证；
- Android TRTC 异步进房、cleanup、音频所有权、蓝牙/耳机/锁屏/后台/网络切换；
- 当前源码可归因的签名 APK；
- packaged sidecar 实际启动、TRTC readiness 和连续两轮 Android + Windows 语音。

这些证据必须来自目标设备、当前工件和独立 reviewer，不能由本 Harness 伪造。
