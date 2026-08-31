# Legacy Runtime Migration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Safely migrate one offline legacy flat sidecar runtime into an ADR-027 stable-root runtime without deleting forensic evidence or permitting an unsafe lock/consumer state.

**Architecture:** Add a small migration boundary that accepts explicit runtime, backup, lock-owner, and consumer-probe dependencies. It rejects ambiguous ownership, active consumers, non-flat layouts, unsafe backup paths, and any pre-existing backup. Only after these gates pass does it rename the legacy root to a supplied sibling backup path, create the stable root, invoke the existing package publisher, and verify the selected immutable generation. A post-move failure leaves the new root fail-closed and preserves the original bytes in the backup.

**Tech Stack:** Node.js built-in filesystem and test runner; existing ADR-027 package publisher/verifier.

---

### Task 1: Define migration gates

**Files:**
- Create: `scripts/lib/sidecar-runtime-migration.js`
- Test: `scripts/test/sidecar-runtime-migration.test.js`

**Step 1:** Add failing tests for active consumers, ambiguous lock ownership, unsafe backup paths, non-flat input, and successful move/build/verify ordering.

**Step 2:** Run the new test file and verify it fails because the module does not exist.

**Step 3:** Implement the minimal migration boundary with injected probes and callbacks.

**Step 4:** Run the focused suite and verify it passes.

### Task 2: Integrate the production command boundary

**Files:**
- Modify: `scripts/build-sidecar-external-bin.js`
- Test: `scripts/test/sidecar-runtime-migration.test.js`

**Step 1:** Add a failing command-contract test requiring an explicit `--migrate-legacy-runtime` flag and operator-supplied backup path; ordinary build and verify remain unchanged.

**Step 2:** Implement an explicit migration path only. It must not infer deletion targets or silently reclaim a lock.

**Step 3:** Prove the CLI invokes the existing package build then verifier only after the migration gates.

### Task 3: Verify the isolated migration path

**Files:**
- Test: `scripts/test/sidecar-runtime-migration.test.js`
- Test: `scripts/test/sidecar-runtime-protocol.test.js`
- Test: `scripts/test/sidecar-package.test.js`

**Step 1:** Run focused migration and ADR-027 suites.

**Step 2:** Run the production verifier against the unmodified main runtime only after any actual migration; it must report a selected generation rather than a legacy fallback.

**Step 3:** Independently inspect current pointer, selected generation metadata, provenance digest, closed file set, and immutable attributes before packaged release build.

### Task 4: Field acceptance remains separate

After a separately authorized main-worktree migration and clean installer build, execute RP-03/RP-07 and the Windows six-scenario acceptance matrix. No migration test can substitute for actual packaged `jax-pet.exe` launch, OpenWith lineage, CA trust, Credential Manager, TRTC, health, and no-orphan evidence.
