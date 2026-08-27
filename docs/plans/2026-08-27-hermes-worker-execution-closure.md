# Hermes Worker Execution Closure Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn a persisted voice-created agent thread into a supervised Hermes worker lifecycle whose true status can be returned to Qwen without copying any Hermes credential.

**Architecture:** Keep Qwen Realtime as the coordinator. `AgentThreadRegistry` remains the persistent source of truth; a new `HermesWorkerRunner` launches an allowlisted Hermes CLI command with a sanitized task file, inherits only the parent process environment, and streams process state into the registry. The worker never receives an API key in arguments, JSON, logs, or SQLite. The first increment is deliberately read-only (`hermes --help`) so execution, cancellation, persistence, and status semantics are proven before permitting project mutations.

**Tech Stack:** Python 3.11 project venv, asyncio subprocesses, SQLite, existing `rtc_bridge`, Hermes CLI installed under `%LOCALAPPDATA%\\hermes`.

---

### Task 1: Prove Hermes CLI discovery and deny unsafe configuration

**Files:**
- Create: `backend/tests/unit/test_hermes_worker_runner.py`
- Create: `backend/app/brain/hermes_worker_runner.py`

**Step 1: Write the failing test**

```python
def test_runner_refuses_when_hermes_binary_missing(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    runner = HermesWorkerRunner(registry, hermes_bin="missing-hermes")
    thread = registry.spawn("检查项目状态")
    result = asyncio.run(runner.start(thread["thread_id"]))
    assert result["status"] == "failed"
    assert "Hermes CLI" in result["summary"]
```

**Step 2: Run test to verify it fails**

Run: `C:\Users\Administrator\WorkBuddy\监视app\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_hermes_worker_runner.py -q`

Expected: FAIL because `HermesWorkerRunner` does not exist.

**Step 3: Write minimal implementation**

Implement `HermesWorkerRunner.start(thread_id)` to resolve a configured executable path, refuse a missing executable, and update the existing registry state to `failed` with a non-secret summary.

**Step 4: Run test to verify it passes**

Run the same test command.

Expected: PASS.

### Task 2: Prove execution and durable completion status

**Files:**
- Modify: `backend/tests/unit/test_hermes_worker_runner.py`
- Modify: `backend/app/brain/hermes_worker_runner.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_runner_records_running_then_completed(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    runner = HermesWorkerRunner(registry, hermes_bin=sys.executable, command_factory=lambda _: [sys.executable, "-c", "print('ok')"])
    thread = registry.spawn("只读检查")
    await runner.start(thread["thread_id"])
    restored = AgentThreadRegistry(str(tmp_path / "threads.sqlite3")).get(thread["thread_id"])
    assert restored["status"] == "completed"
    assert "完成" in restored["summary"]
```

**Step 2: Run test to verify it fails**

Run: `C:\Users\Administrator\WorkBuddy\监视app\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_hermes_worker_runner.py -q`

Expected: FAIL because no subprocess lifecycle is recorded.

**Step 3: Write minimal implementation**

Use `asyncio.create_subprocess_exec` without a shell. Set `running` before spawn; capture a bounded, sanitized combined output; set `completed` only on exit code zero, otherwise `failed`. Do not pass task text on argv; write a private temporary task file if the real Hermes invocation needs prompt input.

**Step 4: Run test to verify it passes**

Run the same test command.

Expected: PASS.

### Task 3: Prove cancellation is process-aware and persistent

**Files:**
- Modify: `backend/tests/unit/test_hermes_worker_runner.py`
- Modify: `backend/app/brain/hermes_worker_runner.py`
- Modify: `backend/app/brain/agent_thread_registry.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_cancel_terminates_live_worker_and_persists_cancelled(tmp_path):
    registry = AgentThreadRegistry(str(tmp_path / "threads.sqlite3"))
    runner = HermesWorkerRunner(registry, hermes_bin=sys.executable, command_factory=lambda _: [sys.executable, "-c", "import time; time.sleep(60)"])
    thread = registry.spawn("慢任务")
    running = asyncio.create_task(runner.start(thread["thread_id"]))
    await runner.wait_until_running(thread["thread_id"])
    assert await runner.cancel(thread["thread_id"])
    await running
    assert registry.get(thread["thread_id"])["status"] == "cancelled"
```

**Step 2: Run test to verify it fails**

Run the focused test.

Expected: FAIL because live process ownership is not tracked.

**Step 3: Write minimal implementation**

Keep live processes in an in-memory map keyed by thread ID. Cancellation terminates only that owned process, waits with a bounded timeout, and persists `cancelled`. A later `agent_status` comes from SQLite and continues to work after the voice connection is gone.

**Step 4: Run test to verify it passes**

Run the focused test.

Expected: PASS.

### Task 4: Wire runner into `rtc_bridge` tool handling

**Files:**
- Modify: `backend/rtc_bridge/server.py`
- Modify: `backend/tests/unit/test_rtc_bridge_server.py`

**Step 1: Write the failing test**

Add a test that invokes `_handle_agent_tool("spawn_agent_thread", {"user_speech": "只读检查"}, "call-1")`, waits for runner completion with an injected fake worker, and asserts returned JSON contains a valid persistent `thread_id` and actual lifecycle summary.

**Step 2: Run test to verify it fails**

Run: `C:\Users\Administrator\WorkBuddy\监视app\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_rtc_bridge_server.py -q`

Expected: FAIL because the bridge only writes a registry record.

**Step 3: Write minimal implementation**

Create `HermesWorkerRunner` once per `BridgeServer`. On `spawn_agent_thread`, create the record, schedule worker execution, and return the initial queued record. On `steer_agent_thread` cancel action, cancel the owned worker before persisting the new state. Never block the audio receive loop on a worker.

**Step 4: Run test to verify it passes**

Run the same test command.

Expected: PASS.

### Task 5: Verify actual Hermes discovery with a read-only, no-key-leak probe

**Files:**
- Create: `tools/verify_hermes_worker_readonly.py`
- Test: `backend/tests/unit/test_hermes_worker_runner.py`

**Step 1: Write the failing test**

Add a unit test asserting the default command factory has no `shell=True`, does not contain `HERMES_CUSTOM_KKDMX_API_KEY`, and invokes only a fixed allowlisted read-only Hermes command.

**Step 2: Run test to verify it fails**

Run the focused test.

**Step 3: Write minimal implementation**

Implement the verification helper. It must report only: binary availability, environment-variable presence as boolean, exit code, and sanitized status. It must not print task content, full environment, or secrets.

**Step 4: Run verification**

Run: `C:\Users\Administrator\WorkBuddy\监视app\.venv\Scripts\python.exe tools/verify_hermes_worker_readonly.py`

Expected: either `PASS` with Hermes executable and inherited credential availability, or an explicit `BLOCKED` reason. A blocked credential is not bypassed by copying it from another process or file.

### Task 6: Regression and delivery evidence

**Files:**
- Modify: `overview.md`
- Modify: `.workbuddy/memory/2026-08-27.md`

**Step 1: Run static and unit gates**

Run:

```bash
C:\Users\Administrator\WorkBuddy\监视app\.venv\Scripts\python.exe -m compileall -q backend/app/brain backend/rtc_bridge
C:\Users\Administrator\WorkBuddy\监视app\.venv\Scripts\python.exe -m pytest backend/tests/unit/test_hermes_worker_runner.py backend/tests/unit/test_agent_thread_registry.py backend/tests/unit/test_qwen_realtime_bridge.py backend/tests/unit/test_rtc_bridge_server.py -q
```

Expected: all selected tests pass.

**Step 2: Record boundaries**

Record the exact result: unit lifecycle proof, actual Hermes discovery status, what is not yet enabled (non-read-only task execution, user approval transport, Samsung end-to-end validation).

**Step 3: Commit**

Do not create a commit in this repository until the external Git ref deletion mechanism is remediated. Preserve the detached worktree evidence and report changed paths instead.
