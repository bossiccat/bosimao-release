# Commercial Voice Restart Audit - 2026-08-08

## Verdict

`FAIL` for commercial release. The restart cleared the prior Gradle native lock failure and allowed Android compilation, unit tests, and APK assembly. This does not close the release gate: required UI P0 gate artifacts are missing, Token-external hardcoded colors remain, Android lint is blocked by an absent offline artifact, and no real-device three-entry/two-round/nonzero-speaker/P95 barge-in evidence exists.

This audit does not reopen or invalidate previously passed Phase 1, Phase 1.5, Phase 2, Batch 1, or Batch 2 local gates.

## Change Impact Analysis

### Current change surface

- Git status: 53 tracked files modified plus untracked implementation, tests, contracts, locks, generated/build/runtime evidence.
- Primary affected behavior: production voice authentication/device lifecycle, fixed 640-byte audio framing and bounded queues, Android serial lifecycle/playback subscription/three entry points, Node/Electron sidecar SDK adapter, Tauri supervisor, privacy/diagnostics, cross-platform UI state/tokens.
- Shared state: SQLite voice database, credential/nonce/rate-limit state, RTC generation/queue state, Android service/coordinator state, sidecar child-process ownership.

### Highest regression risks

1. Credential issuance, nonce replay, revocation and production fail-closed: high, verified by backend full suite.
2. Audio framing/backpressure and stale generation handling: high, verified by backend full suite.
3. Android cancellation, re-entry, playback subscription and three entry-point convergence: high, verified by 46 JVM tests, not by real device.
4. Sidecar native SDK loading and custom audio signature: high, verified by dependency/runtime/Node tests, not by Android speaker loop.
5. Tauri externalBin supervision: high, verified by 11 Rust tests, not by installed application acceptance.
6. UI token compliance: high, failed because the required deterministic scanner is missing and hardcoded colors remain.

## Command Evidence

| Command | Exit | Result |
|---|---:|---|
| `git status --short && git diff --stat && git diff --name-status` | 0 | 53 tracked files changed; broad uncommitted implementation/test surface |
| Gradle 8.7 `-p mobile-app lintDebug --offline` with locked JDK17/cache | 1 | Source compiled; offline resolution failed for `com.android.tools.lint:lint-gradle:31.6.1` |
| Gradle 8.7 `-p mobile-app testDebugUnitTest --offline --rerun-tasks` | 0 | 46 tests, 0 failed, 0 skipped |
| Gradle 8.7 `-p mobile-app assembleDebug --offline --rerun-tasks` | 0 | 36 tasks executed, debug APK assembled |
| `.venv/Scripts/python.exe -m pytest backend/tests -q` | 0 | 468 passed, 1 upstream deprecation warning |
| `npm --prefix sidecar ls --depth=0` | 0 | Electron 31.7.7 and TRTC SDK 13.4.802-beta.3 resolved |
| pinned Node `--test sidecar/test/*.test.js` | 0 | 9 passed, 0 failed/skipped/todo |
| pinned Node `scripts/verify-sidecar-sdk.js` | 0 | manifest/lock/installed/native package checks pass |
| Cargo `test --manifest-path pet-ui/src-tauri/Cargo.toml` | 0 | 11 passed, 0 failed/ignored |
| `npm --prefix pet-ui run build` | 0 | TypeScript and Vite build pass |
| `pytest backend/tests/contract/test_voice_p0_scope.py -q` | 0 | 4 passed |
| `pytest backend/tests/contract/test_ui_p0.py ...` | 4 | required `test_ui_p0.py` missing; 0 tests run |
| `scripts/check-ui-p0.py` lookup | N/A | required deterministic scanner missing |
| `scripts/e2e_commercial_voice.py` lookup | N/A | required real-device evidence runner missing |

## Test Integrity Gate

| Check | Result | Evidence |
|---|---|---|
| Deleted tests | PASS | no tracked test deletion in current diff |
| Assertion reduction | PASS | affected Python assertions strengthened for 201/full-duplex/session fields; playback tests strengthen no-mute and explicit interrupt assertions |
| New skip/xfail/only/focus | PASS | no executable skip marker added; matched text is an anti-cheating comment |
| Hardcoded self-output | PASS with residual risk | reviewed changed assertions trace to Spec contracts; no evidence of implementation-return-as-oracle |
| Harness/config weakening | PASS | package changes pin exact versions; no coverage/test script weakening observed |
| Held-out immutable gate | FAIL | planned `scripts/check-ui-p0.py` and `test_ui_p0.py` are absent |

## P0 and P1 Defects

### P0-RA-001: UI P0 deterministic gate not implemented

- Violation: Plan Task 12 and Spec section 12 require `scripts/check-ui-p0.py` and `backend/tests/contract/test_ui_p0.py`.
- Evidence: both paths are absent; direct pytest invocation exits 4 with 0 tests.
- Expected: committed scanner and contract test execute with exit 0 and enumerate violations by file and line.
- Class: source/delivery blocker.

### P0-RA-002: Token-external hardcoded colors remain

- Violation: Spec section 8 forbids hex/rgb/rgba literals outside Token definition files.
- Evidence: `pet-ui/src/App.tsx` uses `color: #fff`; `pet-ui/src/components/Settings.tsx` uses `background: #fff`; `pet-ui/src/components/Pet.tsx` uses two `#ffffff` SVG literals; `mobile-app/app/src/main/res/drawable/bg_overlay_ball.xml` uses `#33000000`.
- Expected: semantic token/color resources only. Android bit masks used for alpha math are not counted as business colors.
- Class: source blocker.

### P0-RA-003: Real-device commercial evidence absent

- Violation: AC-11, AC-12, AC-13 and Task 14 require main/overlay/notification independent entry evidence, overlay two consecutive audible rounds, nonzero playback plus speaker route, and barge-in P95 <=300 ms.
- Evidence: no `docs/release` evidence existed before this audit; no `scripts/e2e_commercial_voice.py`; searches find only specifications and explicit statements that evidence is missing.
- Expected: sanitized real-device evidence keyed by session/turn with nonzero capture/uplink/downlink/first remote frame/first nonzero playback/speaker route and measured P95.
- Class: real-device blocker.

### P1-RA-001: Android lint cannot complete offline

- Evidence: `lintDebug --offline` reaches `compileDebugKotlin`, then fails resolving uncached `com.android.tools.lint:lint-gradle:31.6.1`.
- Expected: preseed the exact artifact through an approved clean dependency process, then rerun offline; do not alter source or disable lint.
- Class: environment blocker.

### P1-RA-002: Structural debt above 300 lines

- Current production files above 300 lines include `MainActivity.kt` 410, `backend/app/voice/session.py` 400, `backend/app/capture/session_manager.py` 413, and `backend/relay/relay_client.py` 345.
- This is advisory unless a correctness/contract defect is demonstrated. `MainActivity.kt` is an entry file with substantial UI orchestration and should be split after P0 closure without reopening passed behavior.

## Static Compliance

- Emoji scan: matches only an old deploy test page and downloaded/temp HTML, not current product UI source. No current product emoji functional icon was proven.
- Purple-to-pink gradient scan: zero matches in source patterns.
- AI placeholder scan: zero `Welcome to`, `Lorem ipsum`, or `Sign up today` matches in product UI.
- Hardcoded color scan: failed as P0-RA-002.
- `TODO`/empty implementation scan: no newly relevant P0 production placeholder found in the commercial voice path; legacy Feishu/M3 comments remain outside locked P0 path.

## Generated-Code Failure Modes

| Mode | Result | Evidence |
|---|---|---|
| Happy-path bias | PARTIAL | automated failure/security/recovery tests pass; real device failure matrix absent |
| Silent logic error | PARTIAL | strengthened contract/state/audio tests; real speaker loop remains unobserved |
| Hallucinated dependency/API | PASS locally | npm tree, SDK verifier, Node SDK signature tests, Android compilation, Rust build pass |
| Missing system context | PARTIAL | auth/nonce/rate-limit/privacy covered; production TLS/domain and installed-device context not evidenced |
| Performance blind spot | FAIL release gate | no real-device barge-in P95 or full capacity evidence |
| Silent missing import/result | PARTIAL | builds/tests pass except lint environment blocker |

## Production Readiness Scorecard

| Dimension | Grade | Evidence |
|---|---|---|
| Tests and regression | Bronze | broad automated green, but deterministic UI gate and real-device regression evidence absent |
| Contract | Silver | Spec/OpenAPI/P0 scope contract and exact dependency checks pass |
| Security | Silver local | backend security suite passes; production TLS/domain acceptance not evidenced |
| Accessibility | Bronze | design intent exists; lint/UI gate and end-to-end assistive evidence incomplete |
| Performance | Bronze | no real-device P95 <=300 ms evidence |
| Observability | Bronze | fields/contracts exist; no complete real-device evidence package |
| Release safety | Bronze | Tauri supervisor unit tests pass; installed app/autostart/rollback acceptance incomplete |
| Overall | Bronze | lowest dimension; below commercial Silver threshold |

## Release Decision

`FAIL` - P0 count: 3 open. Source blockers: missing UI P0 gate and hardcoded colors. Environment blocker: Android lint offline artifact missing. Real-device blocker: all required three-entry/two-round/nonzero-speaker/P95 evidence is absent. Local automated gates that passed remain valid evidence and must not be reimplemented or discarded.
