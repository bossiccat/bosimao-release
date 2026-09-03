# Windows Native Publish Coordination Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the unsafe Node.js publish-lock recovery prototype with a Windows-native coordination helper that serializes publisher acquisition, crash recovery, pointer replacement, owner cleanup, and release.

**Architecture:** The Node publisher becomes a thin client. A native helper derives a Local named mutex from a canonical NTFS runtime-root path, holds that mutex over the publisher critical section, and maintains `publish.lock` only as an auditable owner record. The helper must never delete malformed or identity-unknown owner records. `current.json` replacement uses the existing Windows `ReplaceFileW`/`MoveFileExW` path while the same mutex is held.

**Tech Stack:** Rust 2021, `windows-sys`, Windows named mutex APIs, `ReplaceFileW`, `MoveFileExW`, Node.js test workers, Rust unit/integration tests.

---

## Preconditions and Non-Goals

- Work from the clean ADR-027 base `701364c` in a new isolated worktree. Do not build on the uncommitted Node prototype as production code.
- Retain the Node prototype and its failed multi-publisher residue-scanning evidence as a regression artifact only.
- Do not rebuild the packaged Sidecar runtime, installer, or Android app in this plan.
- Do not claim RP-03, RP-07, packaged launch, or Samsung S26 acceptance from unit tests.

### Task 1: Freeze Node prototype to fail closed

**Files:**
- Modify: `scripts/lib/sidecar-publish-lock.js`
- Modify: `scripts/lib/sidecar-pointer-publish.js`
- Modify: `scripts/test/sidecar-publish-lock.test.js`
- Modify: `scripts/test/sidecar-runtime-publish.test.js`

**Step 1: Write the failing tests**

Add tests that prove an existing owner classified as `absent` or `pid-reused` does not get removed by the Node implementation. The call must return `SIDECAR_PUBLISH_LOCK_STALE_OWNER`, preserve `publish.lock`, and leave `current.json` unchanged.

**Step 2: Run the focused tests**

Run:
```bash
env -u NODE_OPTIONS -u ELECTRON_RUN_AS_NODE node --test scripts/test/sidecar-publish-lock.test.js scripts/test/sidecar-runtime-publish.test.js
```

Expected: the new stale-owner tests fail because the prototype attempts recovery.

**Step 3: Implement the minimal fail-closed behavior**

Remove Node-side automatic stale recovery, quarantine scanning, and rename/delete recovery. Keep strict owner parsing and identity diagnostics. On `absent` or `pid-reused`, throw `PublishLockError` with code `SIDECAR_PUBLISH_LOCK_STALE_OWNER`, bounded diagnostic fields, and no mutation of lock, pointer, generations, or staging.

**Step 4: Verify focused tests**

Run the command from Step 2.

Expected: PASS; no automatic Node-side lock removal remains.

**Step 5: Commit**

```bash
git add scripts/lib/sidecar-publish-lock.js scripts/lib/sidecar-pointer-publish.js scripts/test/sidecar-publish-lock.test.js scripts/test/sidecar-runtime-publish.test.js
git commit -m "fix: fail closed on stale sidecar publish lock"
```

### Task 2: Create the native coordination helper contract

**Files:**
- Create: `tools/sidecar-publish-coordination/Cargo.toml`
- Create: `tools/sidecar-publish-coordination/src/main.rs`
- Create: `tools/sidecar-publish-coordination/src/protocol.rs`
- Create: `tools/sidecar-publish-coordination/tests/protocol_contract.rs`
- Modify: `scripts/test/sidecar-publish-coordination.test.js`

**Step 1: Write failing contract tests**

Specify newline-delimited JSON requests and responses for:

```text
acquire { runtime_root, owner, timeout_ms }
publish { lease_id, temporary_path, current_path }
release { lease_id, expected_token }
```

Require response fields `operation`, `success`, `status`, `native_error_code`, and a bounded `diagnostic`. Reject unknown fields, non-RFC3339 creation times, uncanonical paths, and lock IDs from another helper process.

**Step 2: Run tests**

Run:
```bash
C:/Users/Administrator/.cargo/bin/cargo.exe test --manifest-path tools/sidecar-publish-coordination/Cargo.toml
```

Expected: FAIL because the crate and protocol do not exist.

**Step 3: Implement strict protocol parsing only**

Implement exact JSON schemas, structured errors, and no filesystem mutation. The helper must write machine-readable responses to stdout and diagnostics to stderr.

**Step 4: Verify tests**

Run the command from Step 2.

Expected: PASS for schema and diagnostic behavior.

**Step 5: Commit**

```bash
git add tools/sidecar-publish-coordination scripts/test/sidecar-publish-coordination.test.js
git commit -m "feat: add sidecar native coordination protocol"
```

### Task 3: Implement canonical runtime-root mutex acquisition

**Files:**
- Modify: `tools/sidecar-publish-coordination/Cargo.toml`
- Modify: `tools/sidecar-publish-coordination/src/main.rs`
- Create: `tools/sidecar-publish-coordination/src/windows_mutex.rs`
- Create: `tools/sidecar-publish-coordination/tests/mutex_contract.rs`

**Step 1: Write failing tests**

Cover canonical-path normalization and deterministic mutex naming:

```text
Local\\jax-sidecar-publish-<sha256(canonical_runtime_root_utf16)>
```

Test same root spelling variants map to one name; distinct roots do not collide; timeout reports busy; abandoned mutex reports `abandoned` rather than success without recovery checks.

**Step 2: Run tests**

Run:
```bash
C:/Users/Administrator/.cargo/bin/cargo.exe test --manifest-path tools/sidecar-publish-coordination/Cargo.toml mutex_contract
```

Expected: FAIL because no mutex implementation exists.

**Step 3: Implement minimal Windows mutex wrapper**

Use `CreateMutexW`, `WaitForSingleObject`, and `ReleaseMutex`. Map `WAIT_OBJECT_0`, `WAIT_TIMEOUT`, `WAIT_ABANDONED`, and `WAIT_FAILED` to explicit protocol statuses. Keep the handle in a per-process `lease_id` registry. On non-Windows, return unsupported and do not provide a portable lock fallback.

**Step 4: Verify tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add tools/sidecar-publish-coordination
git commit -m "feat: serialize sidecar publishers with named mutex"
```

### Task 4: Make owner creation and stale recovery mutex-protected

**Files:**
- Modify: `tools/sidecar-publish-coordination/src/protocol.rs`
- Create: `tools/sidecar-publish-coordination/src/publish_owner.rs`
- Modify: `tools/sidecar-publish-coordination/src/main.rs`
- Create: `tools/sidecar-publish-coordination/tests/owner_recovery_contract.rs`

**Step 1: Write failing tests**

Test these exact cases inside one held mutex:

1. no owner file -> create owner using create-new semantics;
2. live owner -> busy and preserve bytes;
3. absent owner -> only an `abandoned` acquisition may reclaim after strict schema and identity validation;
4. PID reuse -> reclaim only after creation time and identity token differ;
5. malformed or identity-unavailable owner -> fail closed and preserve bytes;
6. crash residue owner -> next process gets `abandoned`, validates, then either reclaims safely or blocks.

**Step 2: Run tests**

Run:
```bash
C:/Users/Administrator/.cargo/bin/cargo.exe test --manifest-path tools/sidecar-publish-coordination/Cargo.toml owner_recovery_contract
```

Expected: FAIL.

**Step 3: Implement minimal owner lifecycle**

Acquire named mutex before examining `publish.lock`. Write the owner through a same-directory temporary file, flush file contents, then use a native same-volume create/replace operation chosen by the mutex-held state. Keep the owner record until pointer replacement succeeds. Only the native helper deletes the owner before `ReleaseMutex`; delete-owner-then-release is mandatory. Do not scan or delete `publish.lock.*` paths outside the held mutex.

**Step 4: Verify tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add tools/sidecar-publish-coordination
git commit -m "feat: recover sidecar publish owner under mutex"
```

### Task 5: Integrate native pointer replacement under the same lease

**Files:**
- Move or reuse: `tools/sidecar-pointer-replace/src/main.rs`
- Modify: `tools/sidecar-publish-coordination/src/main.rs`
- Modify: `tools/sidecar-publish-coordination/Cargo.toml`
- Create: `tools/sidecar-publish-coordination/tests/pointer_commit_contract.rs`

**Step 1: Write failing tests**

Test `publish(lease_id, temporary_path, current_path)` for:

- existing pointer uses `ReplaceFileW` with write-through;
- missing pointer uses `MoveFileExW` with write-through;
- different volume returns `ERROR_NOT_SAME_DEVICE` with old pointer preserved;
- sharing violation, lock violation, and unable-to-move replacement retry only within the configured bound;
- access denied is not retried;
- caller without a valid lease ID cannot publish.

**Step 2: Run tests**

Run:
```bash
C:/Users/Administrator/.cargo/bin/cargo.exe test --manifest-path tools/sidecar-publish-coordination/Cargo.toml pointer_commit_contract
```

Expected: FAIL.

**Step 3: Implement native commit path**

Port the proven `ReplaceFileW` / `MoveFileExW` behavior from the old helper. Preserve structured native error codes. Require the owner mutex lease to remain live across temporary-pointer write, file flush, pointer replace, owner deletion, and mutex release.

**Step 4: Verify tests**

Run the command from Step 2.

Expected: PASS.

**Step 5: Commit**

```bash
git add tools/sidecar-publish-coordination
git commit -m "feat: commit sidecar pointer under native lease"
```

### Task 6: Add real child-process crash and contention tests

**Files:**
- Create: `scripts/test/sidecar-native-publish-worker.js`
- Create: `scripts/test/sidecar-native-publish-coordination.test.js`
- Modify: `scripts/lib/sidecar-pointer-publish.js`

**Step 1: Write failing tests**

Use IPC barriers and real child processes. Cover:

1. holder child keeps named mutex; second child receives bounded busy timeout and cannot change pointer or owner;
2. holder killed after mutex acquisition, owner write, temporary pointer flush, pointer replacement, and owner delete; next child sees only old/new complete pointer and appropriate abandoned recovery behavior;
3. two recovery children race after a crashed owner; exactly one performs recovery;
4. malformed and identity-unavailable owner records remain untouched;
5. a simulated sharing violation preserves old pointer and reports the native error code;
6. no `publish.lock`, temporary pointer, or helper lease residue remains after a normal publish.

**Step 2: Run tests**

Run:
```bash
env -u NODE_OPTIONS -u ELECTRON_RUN_AS_NODE node --test scripts/test/sidecar-native-publish-coordination.test.js
```

Expected: FAIL because Node is not wired to the helper.

**Step 3: Implement Node client integration**

Replace the Node owner-file lock prototype in production publishing with a single helper session or explicit helper lease protocol. Do not pass arbitrary `processIdentity` objects in production; only test adapters may inject identity resolver behavior. Missing helper on Windows must fail closed.

**Step 4: Verify tests**

Run the command from Step 2 five times.

Expected: all runs pass, with no pointer gaps and no residual coordination artifacts after normal completion.

**Step 5: Commit**

```bash
git add scripts/lib/sidecar-pointer-publish.js scripts/test/sidecar-native-publish-worker.js scripts/test/sidecar-native-publish-coordination.test.js
git commit -m "feat: publish sidecar pointer through native coordinator"
```

### Task 7: Independent Windows validation and release-gate update

**Files:**
- Modify: `docs/decisions/ADR-027-sidecar-generation-pointer-protocol.md`
- Create: `outputs/windows-native-publish-coordination-evidence-2026-08-18.md`
- Modify: `outputs/sidecar-adr027-recovery-audit-2026-08-18.md`

**Step 1: Write acceptance checklist**

Record required evidence: Windows version, NTFS root, helper SHA-256, source commit, native error codes, all child crash barriers, five contention rounds, pointer observations, owner-file observations, and residue scan.

**Step 2: Execute native tests**

Run Rust helper tests and Node child-process tests on Windows. Capture raw test output and hashes, not summaries alone.

**Step 3: Evaluate result**

Only mark Node/native publisher coordination `PASS` if every required case passes. Keep RP-03 `BLOCKED` until actual Windows reader sharing-handle tests pass. Keep RP-07 and packaged/installer gates `NOT_RUN`.

**Step 4: Commit evidence and decision update**

```bash
git add docs/decisions/ADR-027-sidecar-generation-pointer-protocol.md outputs/windows-native-publish-coordination-evidence-2026-08-18.md outputs/sidecar-adr027-recovery-audit-2026-08-18.md
git commit -m "docs: record native sidecar publish coordination evidence"
```

## Final Verification

Run these only after all tasks complete:

```bash
C:/Users/Administrator/.cargo/bin/cargo.exe test --manifest-path tools/sidecar-publish-coordination/Cargo.toml
env -u NODE_OPTIONS -u ELECTRON_RUN_AS_NODE node --test scripts/test/sidecar-publish-lock.test.js scripts/test/sidecar-runtime-publish.test.js scripts/test/sidecar-native-publish-coordination.test.js
git diff --check
```

Acceptance requires all commands to pass, the five-round child contention test to pass, and evidence to show no pointer gap, no deletion of unknown owner data, and no successful publisher result after an unclassified native failure.
