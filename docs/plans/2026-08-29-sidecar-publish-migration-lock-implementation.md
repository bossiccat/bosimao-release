# Sidecar Publish and Migration Lock Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ensure ordinary sidecar publishing and one-time legacy runtime migration share one Windows-safe, stable sibling lock so no publisher can race migration preflight, rename, publish, or verify.

> **2026-08-30 修订**：原计划依赖的 Rust named-mutex helper 经实测不适用于同步脚本
> （租约存活在长驻 helper 进程内，一次性调用会在进程退出时立即释放互斥量）。实现改为
> 目录级跨进程租约，owner 字段与 Rust `Owner` 保持一致。详见
> `docs/plans/2026-08-30-sidecar-lock-implementation-revision.md`。

**Architecture:** The coordination identity is derived from the immutable parent directory plus the fixed runtime directory name, not from the runtime root itself. This keeps the lock identity and owner record stable while the legacy root is renamed to a backup. Both normal `build:sidecar` and `--migrate-legacy-runtime` acquire the same exclusive lease before any runtime read or write and release it only after success or fail-closed cleanup.

**Tech Stack:** Node.js, existing Rust `sidecar-publish-coordination` helper, Windows named mutex, JSON owner record, node:test.

---

### Task 1: Define stable sibling-lock identity and contract

**Files:**
- Create: `scripts/lib/sidecar-runtime-coordination.js`
- Test: `scripts/test/sidecar-runtime-coordination.test.js`
- Reference: `tools/sidecar-publish-coordination/src/coordinator.rs`
- Reference: `tools/sidecar-publish-coordination/src/windows_mutex.rs`

**Step 1: Write failing tests**

Cover a runtime at `C:\\...\\binaries\\jax-rtc-sidecar-runtime` and require the common coordination root to remain its parent directory before and after a simulated runtime rename. Require a non-empty owner record with `token`, `pid`, `process_start_time`, `operation`, and `runtime_name`. Require failed owner identity verification, malformed JSON, missing helper, timeout, or abandoned mutex to return a stable non-empty diagnostic.

**Step 2: Run test to verify it fails**

Run:

```text
node --test scripts/test/sidecar-runtime-coordination.test.js
```

Expected: failure because the module does not exist.

**Step 3: Implement the minimal coordination adapter**

The adapter invokes the compiled coordination helper through a fixed argument array. It passes the parent directory as the helper root and derives a sibling owner path from that stable root, never from `runtimeDir`. It returns a release closure only after helper acquisition is confirmed. Any nonzero exit, malformed JSON, owner mismatch, unavailable process identity, stale/PID-reuse ambiguity, timeout, or mutex abandonment is represented as a fail-closed error.

**Step 4: Run tests to verify it passes**

Run the Task 1 test and confirm all cases pass.

### Task 2: Make ordinary publisher acquire the common lease

**Files:**
- Modify: `scripts/build-sidecar-external-bin.js`
- Modify: `scripts/lib/sidecar-package-build.js` only if needed to keep the lease outside the entire publication transaction
- Test: `scripts/test/sidecar-runtime-migration-command.test.js`

**Step 1: Write failing test**

Prove ordinary `buildPackage(config)` cannot begin until `acquireRuntimeCoordination(config.runtimeDir, 'publish')` succeeds, and release occurs after pointer publication/trust verification or error cleanup. Prove a lock failure preserves the stable fail-closed diagnostic and does not call `buildPackage`.

**Step 2: Run test to verify it fails**

Run the focused build command test and confirm the publisher still runs without the shared lock.

**Step 3: Implement minimal wiring**

Acquire before any runtime layout, staging, generation finalization, pointer update, or verification. Use `try/finally`; release failure must be surfaced without replacing a prior operation failure.

**Step 4: Run test to verify it passes**

Run the focused build command test.

### Task 3: Make migration use the same lease rather than a separate lock

**Files:**
- Modify: `scripts/lib/sidecar-runtime-migration-command.js`
- Modify: `scripts/build-sidecar-external-bin.js`
- Modify: `scripts/lib/sidecar-runtime-migration.js`
- Test: `scripts/test/sidecar-runtime-migration.test.js`

**Step 1: Write failing test**

Inject a common lease adapter and assert the exact sequence:

```text
acquire shared migration lease -> validate legacy root -> probe consumers -> verify lock ownership -> rename -> publish -> verify -> release shared lease
```

Assert a simulated ordinary publish attempt receives `BUSY` while the migration lease is held. Assert a lease acquisition failure does not rename the runtime.

**Step 2: Run test to verify it fails**

Run migration tests and confirm the independent `.migration-lock` protocol does not protect normal publish.

**Step 3: Implement minimal wiring**

Remove the obsolete independent lock acquisition from the migration path. Use the same stable sibling coordinator used by normal publish. Preserve the backup and remove `current.json` if post-move work fails.

**Step 4: Run test to verify it passes**

Run migration tests and the command tests.

### Task 4: Verify actual helper lifecycle on Windows

**Files:**
- Test: `scripts/test/sidecar-runtime-coordination.windows.test.js`
- Reference: `tools/sidecar-publish-coordination/tests/*`

**Step 1: Write Windows-only failing integration test**

Use a temporary sibling runtime parent. Start one holder process through the coordination helper, verify a second helper invocation cannot acquire the same lease, terminate the holder, then verify a clean acquisition is possible only with a verifiable owner transition.

**Step 2: Run test to verify it fails**

Run only on Windows with the real helper. Expected initial failure until helper invocation protocol is connected.

**Step 3: Implement or correct helper command wiring**

Do not silently reclaim malformed, identity-unavailable, abandoned, or ambiguous owner state. Only a confirmed dead owner whose creation identity differs from the current PID identity may be reclaimed within the mutex.

**Step 4: Run test to verify it passes**

Run the Windows integration test and record helper JSON output.

### Task 5: Run focused verification and independent review

**Files:**
- Test: `scripts/test/sidecar-runtime-consumer-probe.test.js`
- Test: `scripts/test/sidecar-runtime-migration-command.test.js`
- Test: `scripts/test/sidecar-runtime-migration.test.js`
- Test: `scripts/test/sidecar-runtime-protocol.test.js`
- Test: `scripts/test/sidecar-pointer-replace.test.js`

**Step 1: Run full focused suite**

```text
node --test scripts/test/sidecar-runtime-consumer-probe.test.js scripts/test/sidecar-runtime-coordination.test.js scripts/test/sidecar-runtime-migration-command.test.js scripts/test/sidecar-runtime-migration.test.js scripts/test/sidecar-runtime-protocol.test.js scripts/test/sidecar-pointer-replace.test.js
```

**Step 2: Verify formatting and scope**

```text
git diff --check
git status --short
```

**Step 3: Independent review**

Require a reviewer to check that both publisher and migration acquire the identical stable sibling lock, the owner record survives runtime rename, and ambiguous owner identity blocks rather than permits migration.

**Step 4: Do not migrate main runtime**

Even after code tests pass, main-worktree migration remains blocked until Windows reparse tests, real Windows probe integration, helper lifecycle evidence, installer/clean-install, and packaged six-scenario acceptance have passed.
