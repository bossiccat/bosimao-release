#!/usr/bin/env python3
"""O-018 slice 2 static leakage scanner.

Scans the provision-chain sources and the generated NSIS script for
leakage indicators per windows-sidecar-credential-contract.md §6.1:
  - hardcoded hex secret literals (32+ hex chars)
  - secret read from environment by the NSIS installer
  - Authorization/userSig tokens in the installer script
  - print-like macros that would echo secret-bearing variables
Exit 0 = clean, 1 = findings. Prints stable marker O018_LEAKAGE_SCAN=PASS|FAIL.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TARGETS = [
    "pet-ui/src-tauri/installer/o018-installer-hooks.nsh",
    "pet-ui/src-tauri/src/bin/provision_sidecar_credential_launcher.rs",
    "pet-ui/src-tauri/src/bin/provision_sidecar_credential.rs",
    "pet-ui/src-tauri/src/provision_orchestrator.rs",
    "pet-ui/src-tauri/src/provision_orchestrator_windows.rs",
    "pet-ui/src-tauri/target/release/nsis/x64/installer.nsi",
]

HEX32_RE = re.compile(r"[0-9a-fA-F]{32,}")
# known-benign hex contexts: ADR-027 generation dir hash (g-<hex>), git
# commit hashes inside URLs (blob/<hex>), sha256:<hex> of public artifacts
BENIGN_HEX_RE = re.compile(
    r"g-[0-9a-fA-F]{32,}|blob/[0-9a-fA-F]{32,}|commit/[0-9a-fA-F]{32,}"
    r"|sha256[:=][0-9a-fA-F]{32,}"
)
ENV_SECRET_RE = re.compile(r"ReadEnvStr|\$%[A-Za-z_]*SECRET|ENV_SECRET", re.IGNORECASE)
TOKEN_RE = re.compile(r"Authorization|userSig|UserSig|api[_-]?key\s*=", re.IGNORECASE)
PRINT_RE = re.compile(
    r"(println!|eprintln!|print!|dbg!|log::\w+!)\s*[^;]*"
    r"(secret|credential|opaque|token)",
    re.IGNORECASE,
)
# NSIS DetailPrint status lines are install-progress text; the secret value
# never exists in installer context. Flag only non-DetailPrint leaks there.
NSIS_STATUS_RE = re.compile(r"^\s*(DetailPrint|;\s*)", re.IGNORECASE)


def scan() -> int:
    findings: list[str] = []
    for rel in TARGETS:
        p = REPO_ROOT / rel
        if not p.exists():
            findings.append(f"MISSING target: {rel}")
            continue
        is_nsis = rel.endswith((".nsi", ".nsh"))
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            hex_probe = BENIGN_HEX_RE.sub("", line)
            if HEX32_RE.search(hex_probe):
                findings.append(f"HEX32 {rel}:{i}: {line.strip()[:120]}")
            if ENV_SECRET_RE.search(line):
                findings.append(f"ENV_SECRET {rel}:{i}: {line.strip()[:120]}")
            if TOKEN_RE.search(line):
                findings.append(f"TOKEN {rel}:{i}: {line.strip()[:120]}")
            if PRINT_RE.search(line):
                findings.append(f"PRINT_LEAK {rel}:{i}: {line.strip()[:120]}")
            elif is_nsis and re.search(
                r"\b(secret|credential|opaque)\b", line, re.IGNORECASE
            ) and not NSIS_STATUS_RE.match(line) and not line.strip().startswith("DetailPrint"):
                findings.append(f"NSIS_SECRET_WORD {rel}:{i}: {line.strip()[:120]}")
    if findings:
        print(f"O018_LEAKAGE_SCAN=FAIL ({len(findings)} findings)")
        for f in findings:
            print(f"  {f}")
        return 1
    print("O018_LEAKAGE_SCAN=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(scan())
