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

!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "Provisioning sidecar credential (O-018 slice 2)..."
  ExecWait '"$INSTDIR\provision_sidecar_credential_launcher.exe"' $R0
  IntCmp $R0 0 o018_provision_ok
  DetailPrint "Credential provisioning failed (exit $R0); aborting install."
  Abort
  o018_provision_ok:
    DetailPrint "Sidecar credential provisioned (O-018 slice 2)."
!macroend
