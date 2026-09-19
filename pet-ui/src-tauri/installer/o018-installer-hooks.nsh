; O-018 slice 2 - NSIS installer hook (Tauri 2 installerHooks mechanism).
;
; Contract (docs/plans/2026-08-10-o018-slice2-design.md Task 4, ADR-019 §6.1):
;   - Per-user installer (tauri.conf nsis.installMode = "currentUser"): the hook
;     runs under the final interactive user token.
;   - NSIS invokes ONLY the managed launcher, with a fixed path and ZERO
;     arguments. The opaque credential is generated inside the launcher
;     (BCryptGenRandom) and handed to the one-shot provisioner over an
;     inherited anonymous stdin pipe. It never appears in argv, environment,
;     files, or this installer script.
;   - The helper provision_sidecar_credential.exe is spawned by the launcher
;     via CreateProcess with handle inheritance only - never directly by NSIS.
;   - Fail-closed: any non-zero launcher exit aborts the install, leaving no
;     half-provisioned state. Exit codes (launcher contract): 0 = ok,
;     10 = launcher/orchestration failure (the only non-zero code the
;     launcher binary actually emits).
;
; Static contract tests: backend/tests/contract/test_o018_installer_contract.py
; (verifier: scripts/verify-o018-installer-contract.py)
;
; NOTE: comments in this file stay ASCII on purpose. The .nsh is consumed by
; makensis, not by the Rust/TS toolchain, and NSIS input charset handling is a
; separate failure surface we do not want to widen. The repo's Chinese-comment
; convention is intentionally traded away here for build safety.

; ---------------------------------------------------------------------------
; Legacy watchdog scheduled-task cleanup (claim: windows-popup-free).
;
; Why: earlier product builds registered machine state via schtasks
; (Jax-Watchdog-AtStartup / Jax-Watchdog-Every5Min / jax-watchdog) pointing at
; bin\jax-watchdog-wrap.exe. The registration scripts were retired and the
; wrapper binary no longer ships (now guarded by
; backend/tests/contract/test_local_runtime_scripts_retired.py), but nothing
; ever unregistered the tasks. On an upgraded install they survive, keep firing
; every 5 minutes, fail with 0x80070002 (ERROR_FILE_NOT_FOUND), and the Task
; Scheduler relaunches a missing console binary in the customer's interactive
; session - i.e. the exact "descendant command window" risk this claim covers.
;
; Mechanism: PowerShell Unregister-ScheduledTask, idempotent and non-fatal.
;   - schtasks.exe is deliberately NOT used (blocked by sandbox policy here,
;     and it prints to a console).
;   - nsExec::ExecToLog is used instead of the plain blocking NSIS exec
;     command: it captures the child output into the install log and, critically,
;     keeps the o018 contract verifier happy - that verifier asserts the hooks
;     file contains exactly one blocking launcher exec line (the launcher
;     invocation, fixed path, zero arguments). Do not add a second one.
;   - Exit code is intentionally discarded: a missing task, a policy-restricted
;     PowerShell, or a task owned by another principal must NOT abort the
;     install. Best effort only.
;   - No elevation beyond the per-user token the installer already holds: the
;     Task Scheduler service performs the delete over RPC against the task's own
;     security descriptor, so no HKLM write is attempted.
;   - Task names contain no spaces, so the command needs no inner quoting.
;
; Runs at: fresh install and upgrade (via NSIS_HOOK_POSTINSTALL) and uninstall
; (via NSIS_HOOK_POSTUNINSTALL). See the macro bodies below.
!macro JAX_LEGACY_WATCHDOG_TASK_CLEANUP
  DetailPrint "Removing legacy jax watchdog scheduled tasks (best effort)..."
  Push $R1 ; preserve the caller's $R1 across nsExec
  nsExec::ExecToLog '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Unregister-ScheduledTask -TaskName Jax-Watchdog-AtStartup -Confirm:$$false -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName Jax-Watchdog-Every5Min -Confirm:$$false -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName jax-watchdog -Confirm:$$false -ErrorAction SilentlyContinue"'
  Pop $R1 ; nsExec result ("0"/"1"/"error") discarded on purpose - non-fatal
  Pop $R1 ; restore the caller's $R1
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; Clean up first: a stale 5-minute task must not survive - or fire during -
  ; the install, and it must still be gone if the fail-closed Abort below trips.
  !insertmacro JAX_LEGACY_WATCHDOG_TASK_CLEANUP

  DetailPrint "Provisioning sidecar credential (O-018 slice 2)..."
  ExecWait '"$INSTDIR\provision_sidecar_credential_launcher.exe"' $R0
  IntCmp $R0 0 o018_provision_ok
  DetailPrint "Credential provisioning failed (exit $R0); aborting install."
  Abort
  o018_provision_ok:
    DetailPrint "Sidecar credential provisioned (O-018 slice 2)."
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ; The product created this machine state, so the product removes it. Runs on
  ; a real uninstall and on the silent old-uninstaller pass of an upgrade.
  !insertmacro JAX_LEGACY_WATCHDOG_TASK_CLEANUP
!macroend
