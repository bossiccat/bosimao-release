ALTER TABLE pending_session_claims ADD COLUMN generation INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS control_plane_hello_proofs (
    jti TEXT PRIMARY KEY,
    nonce_hash TEXT NOT NULL UNIQUE CHECK (length(nonce_hash) = 64),
    proof_hash TEXT NOT NULL UNIQUE CHECK (length(proof_hash) = 64),
    session_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    sidecar_user_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 0),
    kid TEXT NOT NULL,
    issuer TEXT NOT NULL,
    audience TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    audio_format_json TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL CHECK (expires_at > issued_at),
    consumed_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES control_plane_sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_hello_proofs_session_generation
    ON control_plane_hello_proofs(session_id, generation);
CREATE INDEX IF NOT EXISTS idx_hello_proofs_expiry
    ON control_plane_hello_proofs(expires_at, consumed_at);
