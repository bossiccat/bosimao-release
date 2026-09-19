# Windows Command-Window P0 — Unblocked Next Steps

## Why progress appeared stuck

The prior work was waiting on task entries that display as in progress but whose metadata is already marked cancelled. In addition, this workspace cannot reliably retrieve Task Scheduler records: the first read was blocked by the system-tool policy and an approved exact-name PowerShell query returned no records. That is an environment evidence gap, not proof that the tasks are absent.

> **Superseded in part (2026-09-19).** The "unclassified provider error" behind Gate 0 was not an
> unclassifiable failure: it was the task-not-found signal, misreported as a query failure by a
> defect in the field-evidence script itself. See "Correction" below. With that defect fixed and
> a positive control in place, this session does yield a classifiable reading.

## Completed source containment

| Legacy route | Current containment | Evidence |
|---|---|---|
| Interactive `Jax-Watchdog-*` scheduled tasks | Registration script is retirement-only and targets only three exact root task names | Scheduled-task retirement regression |
| `watchdog-check.ps1` | Retired compatibility stub; cannot spawn or kill processes | Electron watchdog retirement regression |
| `watchdog-sidecar.ps1` | Retired compatibility stub; cannot loop, spawn or kill processes | Electron watchdog retirement regression |
| Tauri packaged sidecar | Tauri supervisor uses `CREATE_NO_WINDOW` and strips unsafe Electron/Node environment | Existing Rust supervisor implementation |

Latest source-level regression: **21 passed, 0 failed**.

## Correction (2026-09-19): the field-evidence script could not tell "absent" from "query failed"

`windows-popup-field-evidence.ps1` — the official evidence script for this claim — had a real
defect in its task-existence catch:

```powershell
catch [Microsoft.Management.Infrastructure.CimException] { ... }
```

`Get-ScheduledTask -TaskName <non-existent>` throws
`Microsoft.PowerShell.Cmdletization.Cim.CimJobException`, which does **not** derive from
`CimException`. Measured on this machine:

```
[Microsoft.Management.Infrastructure.CimException]::
    IsAssignableFrom([Microsoft.PowerShell.Cmdletization.Cim.CimJobException])  ->  False
```

Consequence, measured before the fix: all three legacy names returned `status = "query_error"`;
the `not_found` branch was **dead code**. The failure mode was silent — a genuine "the task is
absent" was reported as a query failure, indistinguishable from a permission or provider
outage. **The Gate 0 `INACCESSIBLE` recorded above was produced by this defect, not by the
session.**

The script has been rewritten so that absence requires **two independent signals agreeing, with
both controls built in**, and so that a broken channel fails closed instead of collapsing:

| verdict | meaning |
|---|---|
| `LEGACY_TASKS_PRESENT` | at least one legacy name was found (distinct from `INACCESSIBLE`) |
| `LEGACY_TASKS_ABSENT` | all three absent, indistinguishable from a fabricated absent control, and the positive control was visible |
| `INACCESSIBLE` | enumeration/provider cannot be trusted — **not** evidence of absence |

The last output line is machine-consumable: `TASK_ABSENCE_EVIDENCE=<verdict>`.
The script stays read-only and does **not** use `schtasks.exe` (denied by the System Tools
policy, and it reports in the console code page).

Guarded by:

- `scripts/field-evidence/test_popup_field_evidence_ablation.ps1` — 8 injected-provider
  scenarios (changed message, changed exception type, uniform failure, broken enumeration,
  empty enumeration, a task actually present, an empty object returned), currently 8/8 PASS;
- `backend/tests/contract/test_windows_popup_field_evidence_contract.py` — structure plus a
  mutation check that turns the ablation red if the criterion degrades back to a single signal.

**Reading obtained this round (2026-09-19, controls included):** positive control
`360ZipUpdater` = EXISTS, 14 root tasks enumerated, all three legacy names absent
⇒ `TASK_ABSENCE_EVIDENCE=LEGACY_TASKS_ABSENT`.

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

1. On the actual customer or acceptance Windows desktop session, run `windows-popup-field-evidence.ps1` in Windows PowerShell. It only reads the three exact task names and emits a redacted status record whose last line is `TASK_ABSENCE_EVIDENCE=<verdict>`. **Accept only `LEGACY_TASKS_PRESENT` or `LEGACY_TASKS_ABSENT` as a task-existence conclusion** — `INACCESSIBLE` means the channel could not be trusted, and the run must be repeated from a session where the positive control is visible. Quote the positive/negative control fields alongside the verdict: a verdict without a visible positive control is not evidence.
2. If a historical task is found, run `scripts/install-scheduled-tasks.ps1` from that same authorized session, then run the field evidence script again. Both before and after records are mandatory.
3. Launch only the packaged `jax-pet.exe` path. Do not use `start-all.ps1`, any `watchdog*.ps1`, Electron development tooling, or a direct Python command as the acceptance path.
4. Record these six dynamic scenarios: first launch; desktop App restart; forced relay recovery; forced rtc-bridge recovery; two five-minute historical watchdog intervals; normal exit then relaunch.
5. For every scenario capture: start/end UTC, visible command-window observation, product PID, parent PID, session ID, executable name, and `/health` response. A visible product-descendant console, a recreated legacy task, or an orphan process is P0 FAIL.

## Hard boundary

No one may claim the customer popup issue is fixed until the target Windows session has both exact task before/after evidence and the six-scenario runtime evidence above. Static tests prove the legacy source routes have been contained; they do not prove historical installation residue is gone.
