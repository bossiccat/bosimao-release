-- Atomic sidecar control-plane claims. Metadata only; no audio or credentials.
CREATE TABLE IF NOT EXISTS pending_session_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_session_claims_session
    ON pending_session_claims(session_id);
CREATE INDEX IF NOT EXISTS idx_pending_session_claims_available
    ON pending_session_claims(consumed_at, expires_at, created_at);
