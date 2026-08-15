# Release Governance Harness Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将波斯猫官方发布入口改为由受版本控制的 P0 Claim、证据校验和 CI 保护共同控制；没有可归因、未过期、已复核的证据则不能生成官方 release manifest 或取得发布凭据。

**Architecture:** 仓库内新增一个只依赖 Python 标准库的 release-governance 控制面：版本化 policy、claim 和 command lock 描述“什么必须被证明”；纯函数模型校验状态与证据；唯一 preflight CLI fail-closed 生成 manifest。CI 只允许 tag 发布任务调用该 CLI，并且发布凭据只注入该受保护 job。它只能硬阻断官方发布工件，不能拦截 WorkBuddy 平台的 TaskUpdate、模型写文件或直接复制文件；这些需平台级 hook/MCP policy 和人工审批。

**Tech Stack:** Python 3.11 标准库、pytest、JSON、现有 Node sidecar verifier、Cargo/Tauri、GitHub Actions（若仓库实际托管在其他平台，按等价的受保护分支、required status、protected environment 和 CI-only secret 映射）。

---

## 不可伪造的边界

- 本计划绝不把本地 `python scripts/release-preflight.py` 视为不可绕过；本地只提供快速反馈。
- 只有远端受保护 tag 的 CI job 能持有发布凭据、上传 release manifest 或创建官方 release；需在托管平台配置分支保护、CODEOWNERS、required status 和 environment reviewer。
- WorkBuddy 的任务状态、工具权限、直接文件写入、sandbox bypass 和跨会话失败次数，不是项目仓库可截获的资源。它们需平台在 TaskUpdate/TaskComplete 和工具调用前接入 verifier 或 MCP policy proxy；未接通前任何 WorkBuddy“完成”只可视为工作状态，不能视为发布许可。
- 真机、Task Scheduler、clean VM、签名证书与人工身份仍属于外部证据。harness 只校验证据结构、哈希、有效期、版本绑定和 reviewer，不伪造现场 PASS。

## Claim 与证据契约

每个 P0 Claim 必须以独立 JSON 存放在 `governance/claims/<claim_id>.json`。允许状态：`Draft`、`Ready`、`Running`、`EvidencePending`、`Review`、`Verified`、`Rejected`、`Blocked`、`Cancelled`、`CircuitOpen`、`Escalated`。

允许迁移：

```text
Draft -> Ready -> Running -> EvidencePending -> Review -> Verified
Review -> Rejected | Blocked
Rejected -> Ready | CircuitOpen
CircuitOpen -> Escalated
Cancelled 只能由非 Verified 状态进入，且必须声明 superseded_by
```

`Verified` 的 P0 Claim 同时需要：

- `target.artifact_commit` 等于当前被发布 commit；
- `target.artifact_sha256` 等于 CI 计算的工件哈希；
- 至少一条未过期、哈希可校验、绑定 claim/commit/artifact 的证据；
- 至少一条关键失败或拒绝路径证据；
- reviewer 不是 owner，review 状态为批准；
- 未被取消或 supersede；
- 同一 `root_cause_key + verification_method + target` 的连续失败超过阈值时，已进入 CircuitOpen/Escalated，不能直接重试。

## Task 1: 建立受版本控制的治理数据模型和 RED 测试

**Files:**
- Create: `governance/release-policy.json`
- Create: `governance/command-lock.json`
- Create: `governance/claims/windows-popup-free.json`
- Create: `governance/claims/android-duplex-audio.json`
- Create: `scripts/release_governance/__init__.py`
- Create: `scripts/release_governance/model.py`
- Create: `scripts/test_release_governance/test_model.py`

**Step 1: 写失败测试，要求 P0 Verified Claim 必须满足完整绑定**

在 `test_model.py` 构造最小 policy/claim/evidence。覆盖：缺 `artifact_sha256`、证据过期、reviewer 等于 owner、Cancelled 无 `superseded_by`、非法迁移、P0 state 非 Verified。断言所有情形均报出稳定的错误码，而不是返回 true。

**Step 2: 运行 RED**

Run:

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance/test_model.py -q
```

Expected: FAIL，因 `scripts.release_governance.model` 尚不存在。

**Step 3: 写最小 policy 与 Claim 样例**

`release-policy.json` 必须包含：

```json
{
  "schema_version": 1,
  "required_claim_ids": ["windows-popup-free", "android-duplex-audio"],
  "allowed_evidence_kinds": ["ci-command", "windows-field", "android-field"],
  "max_attempts_same_fingerprint": 2,
  "max_evidence_age_hours": 72,
  "required_checks": ["sidecar-verify", "tauri-release-build"],
  "release_channel": "production"
}
```

Claim 样例必须先处于 `EvidencePending` 或 `Blocked`，不得伪造 `Verified`。`windows-popup-free` 明确引用六场景和三个 exact legacy task 的现场证据要求；`android-duplex-audio` 明确引用当前 commit/APK/sidecar/设备绑定、连续两轮音频与故障恢复要求。

`command-lock.json` 的每条 check 固定：`id`、`cwd`、`argv`、`timeout_seconds`、`expected_exit`、`evidence_class`。不允许 shell 字符串、调用方追加参数、相对 cwd 越界或未列白名单命令。

**Step 4: 实现纯模型**

在 `model.py` 仅用 `json`、`datetime`、`hashlib`、`pathlib`：

- `ValidationError(code, message)`；
- `attempt_fingerprint(root_cause_key, verification_method, target)`；
- `validate_claim_shape()`；
- `validate_transition(previous_state, next_state)`；
- `validate_verified_claim(claim, policy, now_utc, expected_commit, expected_artifact_sha256)`；
- `validate_cancelled_claim()`。

禁止读取网络、执行命令或修改文件。所有时间按 UTC ISO-8601 解析失败即拒绝。

**Step 5: 运行 GREEN 和全量相关回归**

Run:

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance/test_model.py -q
```

Expected: PASS，覆盖全部非法状态与证据情形。

**Step 6: Commit**

```bash
git add governance scripts/release_governance scripts/test_release_governance
git commit -m "feat: add release claim validation model"
```

## Task 2: 以 fail-closed verifier 校验完整 Claim 集合

**Files:**
- Create: `scripts/release_governance/verify.py`
- Create: `scripts/test_release_governance/test_verify.py`
- Modify: `governance/release-policy.json`

**Step 1: 写 RED 测试**

覆盖：required claim 缺失、重复 claim id、P0 未 Verified、evidence hash 不匹配、证据超期、Cancelled 未指定替代任务、失败 fingerprint 已达到阈值却未进入 CircuitOpen/Escalated、dirty worktree。断言 verifier 以错误列表和非零结果表示拒绝，而不是只打印 warning。

**Step 2: 运行 RED**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance/test_verify.py -q
```

Expected: FAIL，因为 verifier 尚不存在。

**Step 3: 实现 verifier**

`verify.py` 只接收显式 policy、claims 路径、commit、artifact sha、now。它必须：

- 枚举 JSON 并拒绝 schema/claim id 破损；
- 通过 Task 1 模型校验每个 Claim；
- 读取证据文件并验证 `raw_sha256` 或 `result_sha256`；
- 以 `git diff --quiet`/`git status --porcelain` 检查 dirty worktree；
- 返回结构化结果 `{ "verdict": "pass|fail", "errors": [] }`；
- 任一错误由调用方转换成非零退出。

不得把文件存在、health 或 smoke 自动升级为 Windows/Android field evidence。

**Step 4: 运行 GREEN**

重复 Step 2；再运行：

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance -q
```

Expected: PASS。

**Step 5: Commit**

```bash
git add scripts/release_governance scripts/test_release_governance governance
git commit -m "feat: fail closed on incomplete release evidence"
```

## Task 3: 锁定检查执行器与不可编辑证据输出

**Files:**
- Create: `scripts/release_governance/run_locked_checks.py`
- Create: `scripts/test_release_governance/test_locked_checks.py`
- Modify: `governance/command-lock.json`

**Step 1: 写 RED 测试**

覆盖：未登记 check、调用方附加参数、cwd 逃逸、命令超时、期望 exit 与实际不符、stdout/stderr hash 写入、环境 fingerprint 和 UTC 时间写入。使用临时可执行 fixture，不执行生产 build。

**Step 2: 运行 RED**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance/test_locked_checks.py -q
```

Expected: FAIL。

**Step 3: 实现 locked runner**

执行器只从 command lock 的 `argv` 列表调用 `subprocess.run(shell=False)`，无任何用户参数拼接。输出必须写至：

```text
artifacts/release-evidence/<release_id>/ci-command/<check_id>/result.json
```

`result.json` 包含 schema、release id、check id、git commit、argv、UTC started/finished、exit code、stdout/stderr SHA-256、environment fingerprint、collector。runner 必须拒绝相同 release id/check 的覆盖。

**Step 4: 运行 GREEN**

重复 Step 2；断言结果文件不能被第二次调用覆盖。

**Step 5: Commit**

```bash
git add scripts/release_governance scripts/test_release_governance governance
git commit -m "feat: lock release checks and evidence output"
```

## Task 4: 实现唯一 release preflight 与 manifest

**Files:**
- Create: `scripts/release-preflight.py`
- Create: `scripts/test_release_governance/test_preflight.py`
- Create: `docs/governance/release-harness.md`

**Step 1: 写 RED 测试**

测试必须使用临时 Git 仓库、真实 commit、临时 policy/claims/evidence/command lock 与确定性 Python fixture，禁止 mock `verify`、locked runner 或 manifest 内部调用。覆盖：

- `verify` 子命令只验证，零 locked-result / manifest 写入；
- required Claim 为 `EvidencePending` 时 `release` 非零退出，且不创建 release 目录或 manifest；
- 第一个 locked check 非零或超时时停止后续 check，且不创建 manifest；
- 全部 Verified 与真实 locked check 通过时，生成唯一 manifest；
- 重复 `release_id` 拒绝，原有 manifest 字节不变；
- 缺失或非法 `release_id`、`artifact_path`、`ci_run_url`，以及 commit / artifact SHA 不匹配或缺失，均 fail-closed 且无 manifest；
- release 前、checks 后写 manifest 前任一时点 dirty worktree 或 HEAD 变化，均 fail-closed；
- 缺失、失败或与发布 commit/release id 不一致的 required check `result.json`，均 fail-closed。

当前仓库 Claims 仍为 `EvidencePending`，因此针对真实项目输入的 `release` 必须失败；这是正确行为，不能用 fixture PASS 覆盖现场未验证状态。

**Step 2: 运行 RED**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance/test_preflight.py -q
```

Expected: FAIL。

**Step 3: 实现唯一入口**

`release-preflight.py` 仅做 CLI 参数解析与编排，所有输入均必须显式指定并通过路径/格式校验：

```bash
python scripts/release-preflight.py verify --policy governance/release-policy.json --claims governance/claims --command-lock governance/command-lock.json --repo-root . --artifact-path <CI待发布工件>
python scripts/release-preflight.py release --release-id <CI不可预测ID> --policy governance/release-policy.json --claims governance/claims --command-lock governance/command-lock.json --repo-root . --artifact-path <CI待发布工件> --ci-run-url <CI运行URL>
```

`release` 的固定 fail-closed 顺序为：

1. 对 `artifact_path` 的**精确字节**在 CI 现场计算 SHA-256；禁止相信调用方提供的 artifact hash、已有 manifest 或声明值；
2. 记录并验证 clean worktree 与 immutable HEAD 快照；无法证明 clean 或无法得到完整 HEAD 即拒绝；
3. 将精确 commit 与计算出的 artifact SHA 传给 Claim verifier；只有全部 required Claim 已 `Verified` 才能继续；
4. 仅按 policy `required_checks` 顺序调用 locked runner；任一非零、超时或启动错误立即终止；
5. 逐一读取本次 release id 的 immutable result.json，验证 `release_id`、`check_id`、`git_commit`、`status=passed`、`exit_code=expected_exit` 与 SHA-256；缺失、重复、失败、hash 不符或与快照 commit 不一致即拒绝；
6. 在写 manifest 前再次验证 worktree clean、HEAD 不变、artifact SHA 不变；任一变化即拒绝；
7. 仅上述条件全部满足时，原子创建：

```text
artifacts/release-evidence/<release_id>/release-manifest.json
```

`release_id` 必须先以原子方式预留，已存在该 release 目录、任一 result 或 manifest 时立即拒绝；manifest 应先在同一父目录建立临时文件、写入并 fsync，再使用不覆盖语义原子提交，且对文件与目录 fsync。崩溃留下的临时文件不得被视为 manifest，重试同 release id 必须拒绝。

manifest 至少列出 policy digest、发布 commit、CI 现场计算的 artifact SHA-256、所有 Claim evidence digest、每个 required check 的 result digest、生成 UTC、CI run URL。禁止接受手工 manifest、调用方 artifact hash 或覆盖已有 release id。

**Step 4: 写边界文档**

`docs/governance/release-harness.md` 必须明确：

- 这会阻断官方发布，不会阻断本机文件复制或 WorkBuddy 标记任务；
- artifact SHA 只能由受保护 CI 对待发布精确字节现场计算；本地输入的路径/哈希/manifest 均不构成 release approval；
- 现场证据的采集人/设备/有效期/脱敏要求；
- 当前 #126/#128/#127/#129/#130/#131/#139/#136/#138 未满足时必须保持未验证；
- `required_checks` 的 immutable result 与 HEAD/工件快照必须同时匹配，单独的 health、smoke 或 result 文件不可放行；
- release 目录预留、manifest 原子提交与 crash residue 的拒绝语义；
- 当场景触发两次失败或 24 小时无新增证据时，必须转 `CircuitOpen/Escalated`。

**Step 5: 运行 GREEN**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance -q
```

Expected: PASS。

**Step 6: Commit**

```bash
git add scripts/release-preflight.py scripts/release_governance scripts/test_release_governance docs/governance
git commit -m "feat: gate releases on verified evidence manifest"
```

## Task 5: 将官方发布凭据锁进 CI 而不是本机

**Files:**
- Create: `.github/workflows/release-governance.yml`
- Create: `.github/CODEOWNERS`
- Modify: `.gitignore`
- Modify: `docs/governance/release-harness.md`

**Step 1: 写 workflow 静态 RED 测试**

新增 `scripts/test_release_governance/test_ci_policy.py`，断言 workflow：

- 仅 `v*` tag 触发 release job；
- PR job 只跑 verify；
- release job 使用 protected `production` environment；
- 发布 secret 只在 release job 的环境作用域出现；
- preflight release 非零即不执行发布步骤；
- upload artifact 包含 evidence 和 manifest。

如果当前托管平台不是 GitHub，先以同等语义替换文件和测试，再实施，不得伪造 GitHub 已启用。

**Step 2: 运行 RED**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance/test_ci_policy.py -q
```

Expected: FAIL。

**Step 3: 实现 workflow 与保护配置说明**

Workflow 应先执行 `python scripts/release-preflight.py verify`；tag job 才执行 `release`。禁止 workflow 自动把未验证 field evidence 改为 Verified。CODEOWNERS 至少覆盖 `/governance/`、`/scripts/release_governance/`、`/scripts/release-preflight.py`、workflow 和 release policy。

`.gitignore` 只忽略生成的 `artifacts/release-evidence/` 原始输出；不得忽略 policy、claims、command lock 或人工现场证据的带 hash JSON 索引。原始敏感录屏和日志存到受权限控制的 artifact store，仓库仅存 digest、最小元数据与访问引用。

文档列出需要仓库管理员在托管平台手工启用的强制项：保护 default branch、限制 tag 创建、CODEOWNERS 必审、release-governance required status、production environment reviewer、仅 CI 可访问发布 token。未完成这些平台配置时，harness 的等级只能是 `LOCAL_ONLY`，不能称为不可绕过。

**Step 4: 运行 GREEN**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance -q
```

Expected: PASS。

**Step 5: Commit**

```bash
git add .github .gitignore docs/governance scripts/test_release_governance
git commit -m "ci: protect release governance gate"
```

## Task 6: 独立反证评审与真实环境开通清单

**Files:**
- Create: `docs/governance/release-harness-verification.md`
- Create: `scripts/test_release_governance/test_adversarial_cases.py`

**Step 1: 写 adversarial RED 测试**

对完整 preflight 输入注入：伪造旧 APK、commit 不匹配、过期 Task Scheduler 证据、同 owner reviewer、取消 Claim 无替代、同失败 fingerprint 第三次重试、手工修改 manifest、只含 `/health` 的 Windows Claim。每种必须被拒绝。

**Step 2: 执行 RED 并补足实现缺口**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance/test_adversarial_cases.py -q
```

Expected: FAIL，直到 verifier 对每个篡改样本 fail-closed。

**Step 3: 运行最终验证**

```bash
C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe -m pytest scripts/test_release_governance -q
node --test scripts/test/sidecar-package.test.js scripts/test/sidecar-packaged-launch.test.js
```

Expected: 所有 harness 测试和既有 sidecar 包装契约通过。即使本地测试通过，现有 Claim 不具现场证据时 `release-preflight.py verify` 应以非零退出；这是正确的 fail-closed 行为。

**Step 4: 记录真实环境与平台前置项**

`release-harness-verification.md` 明确列出不可由仓库完成的事项：

- WorkBuddy 需要 TaskUpdate/TaskComplete 前置 hook；
- 工具调用需 MCP policy proxy，避免模型直接绕过 release CLI；
- 目标 Windows 交互桌面、Android 真机、clean VM、第二用户、签名证书和人工 reviewer；
- 托管平台管理员必须开启保护和 CI-only 发布凭据。

**Step 5: Commit**

```bash
git add docs/governance scripts/test_release_governance
git commit -m "test: reject forged release evidence"
```

## 验收定义

仓库内完成不等于商业发布通过。完成本计划后可声明的最强结论仅为：

> “官方 release 流水线在托管平台保护正确配置时，会对缺失、过期、不可归因、未复核或被取消的 P0 Claim fail-closed；当前波斯猫现场 P0 仍未验证，因此 preflight 必须拒绝发布。”

只有 #126/#128/#127/#129/#131/#119/#120/#139/#130/#86/#87/#136/#138 等真实环境 Claim 取得满足契约的现场证据，并且签名、CI 环境保护及人工审批均已接通，才可以讨论 release approval。
