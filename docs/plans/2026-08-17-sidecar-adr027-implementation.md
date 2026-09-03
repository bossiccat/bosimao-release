# Sidecar ADR-027 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 Sidecar runtime 从双目录 rename 迁移到 ADR-027 规定的 stable root + immutable generation + `current.json` 原子指针协议，并用跨进程 crash、Rust reader、Windows 原子替换和 clean-install 动态证据证明其安全边界。

**Architecture:** `jax-rtc-sidecar-runtime` 永远是稳定根目录；每次发布先在 `staging` 构造并完整校验 `g-<64 lowercase hex>` generation，再把它放入不可变 `generations/`，最后只替换同卷的 `current.json`。Reader 在 shared reader/GC 门内完成 pointer snapshot、lease 创建和 generation 校验，验证后把同一个 generation 路径固定给 supervisor/child；GC 只在 exclusive 门内回收无 lease、非 current 且过期的 generation。Windows pointer replace 通过原生 ReplaceFileW/MoveFileExW 适配，不以 Node `fs.renameSync` 作为 Windows 证据。

**Tech Stack:** Node.js publisher/test workers, Rust/Tauri resolver and supervisor, Windows native file APIs (`ReplaceFileW`/`MoveFileExW`), NTFS ACL/share modes, Tauri bundle resources, child-process crash barriers, clean-install evidence manifest.

---

## 开始前的工作树与证据纪律

- 从新的 clean worktree、唯一父提交和无未提交变更开始。禁止把 `task4-runtime` worktree 的脏改动复制、cherry-pick 或当作实现基线。
- 仅允许把 ADR-027、QA 报告 `outputs/sidecar-adr027-implementation-review-2026-08-17.md`、当前生产/测试源码作为需求输入；QA 报告中列出的 `task4-runtime` 文件只用于定位缺口，不能作为通过证据。
- 不在实施计划落地前修改生产代码；本计划文件是本轮唯一产物。
- 每个任务必须按 RED → 最小实现 → 目标测试 → 独立审查 → commit 执行。测试改动与生产改动同一任务时，先提交 RED 测试（若仓库治理允许）或至少保留独立 RED 输出。
- Node/Linux PASS 只证明协议逻辑和 crash 编排；Rust/Cargo PASS 只证明目标代码编译/测试；Windows native adapter PASS 才证明 Windows replace/share/ACL 语义；installer clean-install PASS 才能进入 packaged claim。任何未取得的现场证据维持 `NOT_RUN`/`EvidencePending`。

### Task 0: 建立干净基线与缺口快照

**Files:**
- Read: `docs/decisions/ADR-027-sidecar-generation-pointer-protocol.md`
- Read: `outputs/sidecar-adr027-implementation-review-2026-08-17.md`
- Read: `scripts/lib/sidecar-runtime-publish.js`
- Read: `scripts/test/sidecar-runtime-publish.test.js`
- Read: `pet-ui/src-tauri/src/main.rs`
- Read: `pet-ui/src-tauri/src/sidecar.rs`
- Read: `pet-ui/src-tauri/build.rs`
- Create: `outputs/sidecar-adr027-baseline-<candidate>.md`

**Step 1: Establish the candidate worktree**

Run: `git status --short --branch` and `git diff --check`
Expected: clean worktree, no task4-runtime files in the candidate diff. If dirty, stop and create a new worktree; do not reset user changes.

**Step 2: Record the baseline contract**

Record current flat runtime paths, `runtime -> backup -> staging -> runtime` sequence, existing fixture test names, Rust fixed-path resolver, and build.rs root-level manifest path. Include QA B-01 through B-08 as the starting blocking set.

**Step 3: Independent review and commit**

Reviewer checks that the candidate parent commit is unique, the baseline snapshot is read-only evidence, and no task4-runtime implementation was imported.

Commit: `chore: establish clean sidecar adr027 baseline`

### Task 1: Define Node protocol types and stable-root layout

**Files:**
- Modify: `scripts/lib/sidecar-runtime-publish.js`
- Modify: `scripts/test/sidecar-runtime-publish.test.js`
- Modify: `scripts/test/sidecar-runtime-publish-worker.js`
- Create: `scripts/test/sidecar-runtime-protocol.test.js` (or extend the existing file only if the repository test convention requires one file)

**Step 1: Write RED schema tests**

Add tests for:

- stable root remains present throughout publish;
- layout is exactly `current.json`, `generations/`, `staging/`, `leases/`, `publish.lock`, and `reader-gc.lock`;
- generation ID matches `^g-[0-9a-f]{64}$` and equals the SHA-256 of provenance bytes;
- `generation.json` contains `schema_version`, `generation`, `manifest_sha256`, and a closed `files` hash set;
- current pointer contains only the ADR schema fields and points to a completed generation;
- old flat `runtimeDir` and timestamp/UUID generation names are rejected without fallback.

Run: `node --test scripts/test/sidecar-runtime-protocol.test.js`
Expected: RED because the current publisher has no stable-root/generation schema.

**Step 2: Implement the minimum protocol model**

Add pure helpers for root paths, ID/hash validation, canonical JSON, generation metadata construction, pointer parsing, and path containment. Change `publishRuntime` so `runtimeDir` means stable root; do not replace or rename the root. Build output is redirected to `staging/<generation-id>-<random>`.

Do not implement GC or Windows native replacement in this task. Expose a narrow `replaceCurrentPointer(temp, current, options)` adapter boundary so later tasks can replace its platform implementation without changing generation construction.

**Step 3: Verify the Node contract**

Run: `node --test scripts/test/sidecar-runtime-protocol.test.js scripts/test/sidecar-runtime-publish.test.js`
Expected: new layout tests PASS; old tests that assert the flat runtime or backup recovery must be rewritten as ADR-027 RED tests, not silently retained as green evidence.

**Step 4: Independent review and commit**

Reviewer checks schema exactness, lower-case hex identity, canonical bytes, no root rename, and no hidden fallback to the flat layout.

Commit: `feat: define immutable sidecar generation layout`

### Task 2: Implement complete staged generation and closed-set verification

**Files:**
- Modify: `scripts/lib/sidecar-runtime-publish.js`
- Modify: `scripts/test/sidecar-runtime-publish.test.js`
- Modify: `scripts/test/sidecar-runtime-publish-worker.js`
- Inspect/modify as needed: `scripts/build-sidecar-external-bin.js`, `scripts/lib/sidecar-package.js`

**Step 1: Write RED closed-set tests**

Cover executable, hash files, provenance, provenance digest, all native dependencies, `generation.json`, and unexpected regular files. Add symlink/reparse-point fixtures and unsupported file-type fixtures. Assert that a rejected input never changes `current.json` and that a completed generation cannot be modified in place.

Run: `node --test scripts/test/sidecar-runtime-protocol.test.js`
Expected: RED because current verification is self-consistent fixture validation and does not verify generation metadata or the full payload set.

**Step 2: Implement staged build/finalize**

Build into `staging`; reject links/reparse points and non-regular payload entries; compute the provenance-based generation ID; write `generation.json`; hash every allowed payload; flush files and supported directory metadata; rename only staging to a previously absent `generations/g-<id>` on the same volume. If the destination already exists, reuse only byte-identical verified content; never overwrite it.

Apply immutable ownership/ACL or read-only protection at the finalization boundary through a platform hook. Node must fail closed when it cannot establish the required protection on a target that claims immutable behavior.

**Step 3: Verify failure atomicity**

Run the focused Node suite. Expected: build/verify failure leaves the previous pointer and generation usable; staging is cleaned; finalized but unpublished generations are harmless and recoverable; no backup directory is created.

**Step 4: Independent review and commit**

Reviewer checks held-out extra file, symlink, malformed metadata, and duplicate-generation inputs, and confirms that tests do not derive expected hashes from the same unchecked data path.

Commit: `feat: finalize verified immutable sidecar generations`

### Task 3: Add current pointer publication and crash recovery

**Files:**
- Modify: `scripts/lib/sidecar-runtime-publish.js`
- Modify: `scripts/test/sidecar-runtime-publish.test.js`
- Modify: `scripts/test/sidecar-runtime-publish-worker.js`
- Create/modify: pointer adapter module selected by the implementation review (for example `scripts/lib/sidecar-pointer-replace.js`)

**Step 1: Write RED pointer/crash tests**

Implement deterministic child barriers for RP-02:

- kill after staging verification;
- kill after generation finalize;
- kill after temporary pointer write/flush;
- kill immediately before and immediately after pointer replace.

The parent records child exit code, barrier timeline, pointer bytes, selected generation, and every reader observation. Assert that observations are only complete `old` or complete `new`; reject `ENOENT`, malformed JSON, path escape, missing file, mixed versions, and temporary pointer reads.

Run: `node --test scripts/test/sidecar-runtime-publish.test.js --test-name-pattern='RP-02|pointer|crash'`
Expected: RED on the current two-rename implementation and any implementation that deletes/replaces the pointer through an observable gap.

**Step 2: Implement platform-neutral pointer protocol**

Write one canonical pointer temp file in the same directory, flush its bytes, invoke the adapter to replace `current.json`, and only then report the publish linearization point. On adapter failure, remove only the temp pointer, retain the old pointer, and return a diagnostic error containing operation, path, and native error code where available. Never call `unlink(current)` before replacement.

Add bounded retry only for classified transient sharing errors; after exhaustion the publish is not committed and the old pointer remains readable.

**Step 3: Verify recovery and rollback semantics**

Run focused Node crash tests. Expected: pre-replace crash leaves old pointer; post-replace crash leaves new pointer; no automatic directory rollback; next publisher removes only known staging/temp garbage. A bad published generation is rolled forward by publishing a new pointer, never by mutating an old generation.

**Step 4: Independent review and commit**

Reviewer inspects the actual call graph for delete-and-rename, pointer temp cleanup, same-volume assumptions, and whether tests can observe the linearization point without timing sleeps.

Commit: `feat: publish sidecar current pointer atomically`

### Task 4: Add leases, reader-GC lock, GC, and publisher identity

**Files:**
- Modify: `scripts/lib/sidecar-runtime-publish.js`
- Modify: `scripts/test/sidecar-runtime-publish.test.js`
- Modify: `scripts/test/sidecar-runtime-publish-worker.js`
- Create/modify: shared lock/lease helper module selected during implementation

**Step 1: Write RED RP-04/RP-05/RP-06 tests**

Use deterministic barriers, not polling races:

- RP-04: reader snapshots old pointer and pauses before lease; publisher switches to new and runs GC; reader resumes and must still acquire/validate old.
- RP-05: reader dies after lease creation but before spawn; GC may remove only a lease whose PID and process creation identity are proven stale; unknown identity is retained.
- RP-06: child remains alive while its reader exits; GC must retain the child generation, including Windows file-handle deletion failures, until child identity/exit is confirmed.

Run: `node --test scripts/test/sidecar-runtime-publish.test.js --test-name-pattern='RP-04|RP-05|RP-06|lease|GC'`
Expected: RED because the current implementation has no leases, reader-GC lock, or GC.

**Step 2: Implement lock and lease ordering**

Inside shared `reader-gc.lock`: read current bytes once, validate pointer, create a unique `leases/g-<id>/<reader-uuid>.json` with PID and process creation time, then validate the full generation. Hold the lease until child exit. GC uses exclusive lock and rechecks current pointer, retention set, lease identity, minimum age, and path ownership before deletion.

Update publish lock owner data with token, PID, and process creation time. Stale recovery may quarantine only provable transaction artifacts; it must never delete a completed generation or current pointer.

**Step 3: Verify GC safety**

Run the focused Node suite. Expected: current generation, two retained previous generations, and any active/uncertain lease are never removed; deletion errors are retained for retry and do not turn a committed publish into failure.

**Step 4: Independent review and commit**

Reviewer checks TOCTOU ordering, PID reuse defense, unknown identity retention, active child lifetime, and that GC cannot delete the current generation after a pointer re-read.

Commit: `feat: protect sidecar generations with leases and GC`

### Task 5: Implement Rust reader snapshot and diagnostic fail-closed resolver

**Files:**
- Create/modify: `pet-ui/src-tauri/src/sidecar_runtime_pointer.rs`
- Modify: `pet-ui/src-tauri/src/main.rs`
- Modify: `pet-ui/src-tauri/src/sidecar.rs`
- Modify: `pet-ui/src-tauri/src/lib.rs` if module registration is required
- Modify: `pet-ui/src-tauri/tests/sidecar_runtime_pointer.rs`
- Create/modify: Rust test fixtures only under `pet-ui/src-tauri/tests/`

**Step 1: Write RED Rust resolver tests**

Add tests for valid current/generation metadata, malformed/truncated pointer, unknown fields, wrong generation ID, pointer/generation/provenance digest mismatch, missing payload, extra payload, traversal, symlink and Windows reparse-point policy. Assert structured diagnostic errors and no fake sentinel paths.

Add a snapshot test that changes `current.json` after resolution and verifies the resulting `SidecarSpec` still points to the originally validated immutable generation.

Run later (not in this planning session): `cargo test --manifest-path pet-ui/src-tauri/Cargo.toml sidecar_runtime_pointer`
Expected before implementation: RED or compile failure for the missing resolver behavior. Current `cargo` availability and all results remain `NOT_RUN` until an allowed build environment exists.

**Step 2: Implement resolver and snapshot ownership**

Parse one pointer read, validate exact schema and path containment, acquire the reader-GC shared gate/lease through a small boundary, validate `generation.json` and the closed payload set, and return a typed `ResolvedRuntime` containing generation root, binary, manifest, expected hashes, and lease guard. Make lease release explicit on child exit.

Change `resolve_sidecar_spec` to return `Result<SidecarSpec, diagnostic error>`; setup must log the stable error and refuse initial start rather than construct `__invalid-sidecar-pointer__` or hide the resolver error in a fallback spec. Set `IntegritySpec.runtime_dir` and `Command::current_dir` to the selected generation.

**Step 3: Verify supervisor integration**

Add tests proving pre-spawn validation, credential load/revalidation, and restart use the same generation snapshot/lease contract. No `current.json` re-read may occur between validation and spawn.

**Step 4: Independent review and commit**

Reviewer checks no fake path, no silent fallback, typed diagnostics, lease lifetime, child current directory, and compatibility with existing SHA/provenance checks.

Commit: `feat: resolve sidecar runtime through immutable snapshot`

### Task 6: Update package generation, build.rs, and Tauri resource layout

**Files:**
- Modify: `scripts/build-sidecar-external-bin.js`
- Modify: `scripts/lib/sidecar-package.js`
- Modify: `scripts/test/sidecar-package.test.js`
- Modify: `pet-ui/src-tauri/build.rs`
- Modify: `pet-ui/src-tauri/tauri.conf.json`
- Modify: package/build fixture tests only as needed

**Step 1: Write RED selected-generation tests**

Assert package output contains stable root, pointer, `generations/g-<64hex>/generation.json`, closed payload set and no flat fallback. Add a test that changes a selected generation payload and expects verification failure. Add a test that a target-triple filename cannot substitute for the required installed `jax-rtc-sidecar.exe` identity.

Assert build.rs watches `current.json`, selected generation metadata/hash/payload and package inputs. Assert release manifest digest is computed from the pointer-selected provenance bytes.

Run later: `node --test scripts/test/sidecar-package.test.js`
Expected before implementation: RED for selected-generation layout and rerun/manifest-source checks.

**Step 2: Implement package/build wiring**

Package verifier must create and validate the initial immutable generation and pointer, then emit the full tree without flattening/symlink-following. Build rerun rules must include pointer, selected generation files, and package sources. Release verification must resolve the pointer and reject missing, escaped, mismatched or extra files.

Keep `externalBin`/bundle resource mapping pointed at the stable runtime root while preserving the full generation tree. Remove old root-level manifest assumptions and do not add silent migration to the unsafe flat layout.

**Step 3: Verify static/package contracts**

Run the Node package suite and diff inspection. Expected: package outputs and build inputs match ADR layout; this remains build-tree evidence only and does not upgrade packaged claim.

**Step 4: Independent review and commit**

Reviewer checks resource mapping, target filename identity, selected-generation manifest source, rerun completeness, no symlink flattening, and no task4-runtime fixture contamination.

Commit: `feat: wire selected sidecar generation into packaging`

### Task 7: Replace the Node pointer adapter with a Windows native atomic implementation

**Files:**
- Create: a narrowly scoped native helper crate/CLI (for example `tools/sidecar-pointer-replace/Cargo.toml`, `tools/sidecar-pointer-replace/src/main.rs`, lockfile as required by repository policy), or the repository-approved equivalent native adapter location
- Modify: `scripts/lib/sidecar-pointer-replace.js`
- Modify: `scripts/lib/sidecar-runtime-publish.js`
- Create/modify: Windows adapter tests under `scripts/test/`
- Modify: build/tooling wiring needed to build the helper reproducibly

**Step 1: Write RED Windows adapter tests**

On a real Windows runner, hold a reader handle with the required share flags while publishing; force access denied/sharing violation; test missing-pointer first publish and existing-pointer replace. Capture native error code, old pointer bytes, new pointer bytes, and whether a reader observed a gap.

Run later on Windows only. Expected before native implementation: RED because Node `fs.renameSync` lacks ReplaceFileW/MoveFileExW write-through and bounded native error handling.

**Step 2: Implement the smallest native boundary**

Expose two operations: replace existing pointer with `ReplaceFileW` and create first pointer with `MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)`. Keep temp and pointer on the same NTFS volume. Return operation and native error code to Node. Retry only classified transient sharing errors with a bounded deadline; never unlink the old pointer as a fallback.

For non-Windows, use same-volume rename plus file/parent-directory sync where supported, but label this as logical portability behavior, not Windows evidence.

**Step 3: Verify Windows semantics**

Run the adapter tests on a clean Windows runner with NTFS and record OS version, volume type, ACL, share mode, native error codes, timeline, pointer hashes, and exit status. Expected: old pointer remains readable on failed replace; no reader sees missing/malformed pointer on successful concurrent publication.

**Step 4: Independent review and commit**

Reviewer must inspect native flags, share modes, error mapping, retry bound, same-volume check, and ensure no delete-and-rename path exists.

Commit: `feat: use native Windows atomic pointer replacement`

### Task 8: Enforce immutable/reparse/ACL behavior and complete crash test suite

**Files:**
- Modify: publisher, resolver, native adapter and supervisor files from Tasks 1-7
- Modify: `scripts/test/sidecar-runtime-publish.test.js`
- Modify: `scripts/test/sidecar-runtime-publish-worker.js`
- Modify: `pet-ui/src-tauri/tests/sidecar_runtime_pointer.rs`
- Create: evidence collector/helper scripts only if required by existing test conventions

**Step 1: Write RED RP-01 and held-out integrity tests**

RP-01 runs a parent reader loop and child publisher. The child is killed at the exact old-runtime/new-runtime gap that makes the current implementation expose `ENOENT`. Parent observations must be complete old/new only. Add held-out malformed generation, reparse-point, extra-file, and pointer-corruption cases that are not generated by the same fixture builder.

Run later: `node --test scripts/test/sidecar-runtime-publish.test.js --test-name-pattern='RP-01'`
Expected before implementation: RED on the clean baseline and any remaining flat/directory-swap implementation.

**Step 2: Implement immutable finalization and Windows reparse/ACL checks**

Reject all links/reparse points across the full payload tree; ensure final generation ownership/ACL prevents runtime writes by the reader identity; preserve readable sharing for pointer replacement; keep deletion failures for GC retry. Do not equate Unix symlink tests with Windows reparse evidence.

**Step 3: Run the full logical suite**

Run Node tests and, in an environment with Cargo, Rust tests. Record exact command, commit SHA, test count, skips/xfails and exit code. Expected: RP-01..RP-06 logical crash/lease tests PASS; RP-03 Windows and RP-07 installer remain `NOT_RUN` until their dedicated stages.

**Step 4: Independent test-integrity review and commit**

Reviewer checks no `.only`, `skip`, `xfail`, timing-only race, self-derived expected hash, static-only substitute, or fixture that bypasses the resolver/publisher boundary.

Commit: `test: close sidecar reader publisher crash contract`

### Task 9: Run Windows dynamic evidence and installer clean-install evidence

**Files:**
- Create: `outputs/sidecar-rp03-<candidate>.json`
- Create: `outputs/sidecar-rp07-<candidate>.json`
- Create: `outputs/sidecar-install-evidence-<candidate>.json`
- Modify production/package files only if a dynamic finding identifies a real implementation defect; each fix returns to the relevant earlier task and gets a new commit.

**Step 1: Execute RP-03 on a clean Windows environment**

Prerequisites: NTFS local volume, controlled ACLs, known OS build, native helper built from the same candidate SHA, no task4-runtime files. Run concurrent publisher/reader and failure injection. Save raw stdout/stderr, exit codes, native error codes, pointer/generation hashes, volume/ACL/share metadata and evidence manifest.

Expected: PASS only if no missing/malformed pointer is observed and failed replace preserves the old pointer. Otherwise verdict remains FAIL and the implementation returns to Task 7/8.

**Step 2: Build the installer from the same candidate**

Run the repository-approved release/package command on a clean build environment. Record candidate SHA, installer SHA, provenance, package tree and build output. Do not use a build-tree fixture as RP-07 evidence.

**Step 3: Clean-install and execute RP-07**

On a fresh Windows install, verify actual `resource_dir/jax-rtc-sidecar-runtime/current.json`, selected generation tree, ACL/share state and no flattened/symlinked payload. Start the Tauri app through the real resolver/supervisor pre-spawn path; record resolver diagnostics, compiled manifest SHA, child current directory, launch result and no-orphan cleanup. Keep Credential Manager, TRTC and no-orphan observations separate and attributable to this installer SHA.

Expected: RP-07 PASS only with real clean-install and launch evidence. If installer, Tauri, sidecar launch, TRTC, or no-orphan evidence is unavailable, mark that item `NOT_RUN` and keep packaged claim `EvidencePending`.

**Step 4: Independent field-evidence review and commit/tag gate**

Reviewer confirms same candidate SHA across source, native helper, installer and evidence files; checks no post-build mutation; verifies raw artifacts and evidence manifest are present. No release claim is upgraded on partial evidence.

Commit: `evidence: record sidecar Windows and clean install gates`

### Task 10: Final claim review and release boundary

**Files:**
- Update: `outputs/sidecar-adr027-implementation-review-<date>.md` or the repository’s current audit artifact
- Update: release status only if all required evidence is present

**Step 1: Reconcile every blocker**

Map B-01 through B-08 to implementation/test/evidence artifact references. Any item without a reproducible artifact remains blocking. Explicitly list Node logical PASS, Rust PASS/NOT_RUN, Windows RP-03 PASS/NOT_RUN, installer RP-07 PASS/NOT_RUN, and Credential Manager/TRTC/no-orphan status.

**Step 2: Apply the claim gate**

- ADR implementation claim may pass only after Tasks 1-8 and independent reviews pass.
- Windows atomic replace claim may pass only after Task 7/Task 9 RP-03 evidence.
- Packaged sidecar claim may pass only after same-candidate installer clean install, Tauri supervisor pre-spawn, sidecar launch and required field evidence.
- Missing Cargo, Windows, installer, device, or topology execution remains `NOT_RUN`; no fixture/static test may upgrade it.

**Step 3: Final independent review and commit**

Reviewer verifies no unrelated dirty changes were included, no evidence was generated from task4-runtime, all commands and exit codes are recorded, and the final verdict is fail-closed when any boundary is incomplete.

Commit: `audit: close sidecar adr027 implementation evidence`

## Expected End State

- Stable root is never renamed or deleted during publish.
- Every visible pointer names a complete, immutable, hash-bound generation.
- Reader gets one validated generation snapshot and keeps a lease through child lifetime.
- GC cannot delete current, retained, active, or identity-uncertain generations.
- Windows pointer replacement uses native atomic replace/create semantics with bounded error handling; Node rename alone is not accepted as evidence.
- Build/package/installer preserve the full selected-generation tree and compiled manifest provenance.
- RP-01..RP-07 are deterministic and auditable; fixture/static tests are supplementary only.
- Any unexecuted Cargo/Windows/installer/field evidence remains explicitly `NOT_RUN` and keeps the corresponding claim `EvidencePending`.
