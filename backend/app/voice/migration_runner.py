"""Atomic SQLite migration runner for commercial voice storage."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .._frozen_paths import bundled_path

MIGRATIONS_DIR = bundled_path("backend", "app", "voice", "migrations")
MIGRATIONS = (
    "001_commercial_voice.sql",
    "002_pending_session_claims.sql",
    "003_credential_identity.sql",
    "004_pending_claim_tokens.sql",
    "005_control_plane_ledger.sql",
    "006_wake_events.sql",
    "007_hello_proofs.sql",
    "008_wake_events_user_sig_cipher.sql",
)


def split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(current))
            current = []
    if current:
        joined = "\n".join(current).strip()
        if joined:
            statements.append(joined)
    return statements


def apply_migrations(conn: sqlite3.Connection,
                     migrations_dir: Path = MIGRATIONS_DIR) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at REAL NOT NULL)"
    )
    applied = {
        row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()
    }
    for migration in MIGRATIONS:
        if migration in applied:
            continue
        script = (migrations_dir / migration).read_text(encoding="utf-8")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in split_sql_script(script):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at)"
                " VALUES (?, strftime('%s','now'))",
                (migration,),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
