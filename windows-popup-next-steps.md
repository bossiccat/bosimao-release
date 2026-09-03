# Windows Command-Window P0 — Unblocked Next Steps

## Why progress appeared stuck

The prior work was waiting on task entries that display as in progress but whose metadata is already marked cancelled. In addition, this workspace cannot reliably retrieve Task Scheduler records: the first read was blocked by the system-tool policy and an approved exact-name PowerShell query returned no records. That is an environment evidence gap, not proof that the tasks are absent.

## Completed source containment

| Legacy route | Current containment | Evidence |
|---|---|---|
| Interactive `Jax-Watchdog-*` scheduled tasks | Registration script is retirement-only and targets only three exact root task names | Scheduled-task retirement regression |
| `watchdog-check.ps1` | Retired compatibility stub; cannot spawn or kill processes | Electron watchdog retirement regression |
| `watchdog-sidecar.ps1` | Retired compatibility stub; cannot loop, spawn or kill processes | Electron watchdog retirement regression |
| Tauri packaged sidecar | Tauri supervisor uses `CREATE_NO_WINDOW` and strips unsafe Electron/Node environment | Existing Rust supervisor implementation |

Latest source-level regression: **21 passed, 0 failed**.

## Immediate field action

### Gate 0: obtain a query-capable Windows session

The current LiteSandbox session is `INACCESSIBLE`: native `schtasks.exe` is denied by the System Tools policy, and `Get-ScheduledTask` reaches an unclassified provider error with no auditable ErrorRecord. This is neither `EXISTS` nor `ABSENT` evidence. Do not retire, delete, disable, or claim absence from this session.

Use a Windows administrator session that can read Task Scheduler records. Before querying any task, preserve UTC timestamp, username, stdout, stderr, exit code, and the full task path. The read-only preflight is:

```powershell
Get-Service Schedule | Format-List Name,Status,StartType
Get-Command Get-ScheduledTask
Get-Command schtasks.exe
whoami /groups
```

For every exact task name, query the full root or folder-qualified path and retain both query forms and all error fields on failure:

```powershell
schtasks.exe /Query /TN "\\<TaskPath>\\Jax-Watchdog-AtStartup" /V /FO LIST
Get-ScheduledTask -TaskPath "\\<TaskPath>\\" -TaskName "Jax-Watchdog-AtStartup" | Format-List *
```

The only allowed conclusions are: successful task object = `EXISTS`; explicit Task Scheduler not-found result from the authorized session = `ABSENT`; policy/permission/provider failure, empty output, or missing ErrorRecord = `INACCESSIBLE` and blocks retirement.

1. On the actual customer or acceptance Windows desktop session, run `windows-popup-field-evidence.ps1` in Windows PowerShell. It only reads the three exact task names and emits a redacted status record.
2. If a historical task is found, run `scripts/install-scheduled-tasks.ps1` from that same authorized session, then run the field evidence script again. Both before and after records are mandatory.
3. Launch only the packaged `jax-pet.exe` path. Do not use `start-all.ps1`, any `watchdog*.ps1`, Electron development tooling, or a direct Python command as the acceptance path.
4. Record these six dynamic scenarios: first launch; desktop App restart; forced relay recovery; forced rtc-bridge recovery; two five-minute historical watchdog intervals; normal exit then relaunch.
5. For every scenario capture: start/end UTC, visible command-window observation, product PID, parent PID, session ID, executable name, and `/health` response. A visible product-descendant console, a recreated legacy task, or an orphan process is P0 FAIL.

## Hard boundary

No one may claim the customer popup issue is fixed until the target Windows session has both exact task before/after evidence and the six-scenario runtime evidence above. Static tests prove the legacy source routes have been contained; they do not prove historical installation residue is gone.
