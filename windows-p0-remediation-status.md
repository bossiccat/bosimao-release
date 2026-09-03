# Windows P0 Command Window Remediation Status

## What was done

- Retired the legacy interactive scheduled-task registration entry point in `scripts/install-scheduled-tasks.ps1`. It now only removes the exact historical root tasks `Jax-Watchdog-AtStartup`, `Jax-Watchdog-Every5Min`, and `jax-watchdog`.
- Retired inherited development Electron watchdog entry points:
  - `scripts/watchdog-check.ps1`
  - `scripts/watchdog-sidecar.ps1`

  Both now fail explicitly with exit code 1 and cannot start `electron.cmd`, create child processes, run a permanent loop, or globally terminate Electron processes.
- Added `scripts/test/legacy-electron-watchdog-retirement.test.js` to prevent those production-risk behaviors from returning.
- Re-ran the combined static regression suite:
  - legacy Electron watchdog retirement
  - legacy scheduled-task retirement
  - v2 redacted window-lineage attribution

  Result: 21 tests passed, 0 failed.

## Key decisions

- Production startup ownership remains limited to `jax-pet.exe` and its Tauri-controlled `SidecarSupervisor`.
- Source-level tests are not accepted as proof that customer command windows have stopped.
- The existing PowerShell/Node window lineage collector is blocked: it exposes raw process/window metadata and has the probe spawn Node. It must be deleted or replaced by a redacted, read-only collector before dynamic certification.

## Remaining release blockers

- The current environment cannot access `Get-ScheduledTask`; exact historical task presence and removal have not been proven on the target Windows customer session.
- A real customer-path dynamic validation is still required: first launch, App restart, forced relay/bridge recovery, two historical five-minute intervals, shutdown/restart, and orphan-process/window lineage checks.
- The Tauri NSIS installer, upgrade, and uninstall artifact closure must still prove that legacy PowerShell watchdogs and `electron.cmd` are not bundled or invoked.

## Release status

Overall commercial release remains **FAIL** until task-scheduler evidence, production artifact closure, and dynamic no-window evidence are complete.
