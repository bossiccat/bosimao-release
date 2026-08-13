-- 001_commercial_voice.sql
-- 波斯猫商业双工语音：SQLite 安全存储初始迁移（SPEC §6 数据模型）
-- 只存哈希/密文；禁止明文 Secret、nonce、userSig、原始音频落库。

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value_encrypted TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_settings_key ON settings(key);

CREATE TABLE IF NOT EXISTS device_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    device_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    credential_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    expires_at REAL NOT NULL,
    last_seen_at REAL,
    revoked_at REAL,
    revoke_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_credentials_device_id ON device_credentials(device_id);
CREATE INDEX IF NOT EXISTS idx_device_credentials_status ON device_credentials(status, revoked_at);

CREATE TABLE IF NOT EXISTS pairing_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash TEXT NOT NULL,
    created_by_owner_id TEXT NOT NULL,
    device_name_hint TEXT,
    platform TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    consumed_device_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pairing_codes_code_hash ON pairing_codes(code_hash);
CREATE INDEX IF NOT EXISTS idx_pairing_codes_expiry ON pairing_codes(expires_at, consumed_at);

CREATE TABLE IF NOT EXISTS revoked_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    user_sig_fingerprint TEXT NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_revoked_sessions_session_id ON revoked_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_revoked_sessions_device ON revoked_sessions(device_id, expires_at);

CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_id TEXT,
    device_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state TEXT,
    error_code TEXT,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_events_lookup ON session_events(session_id, device_id, created_at);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ciphertext BLOB NOT NULL,
    encryption_version TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_session ON transcripts(session_id, created_at);

CREATE TABLE IF NOT EXISTS privacy_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    result TEXT NOT NULL,
    metadata_redacted_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_privacy_audit_events_action ON privacy_audit_events(action, created_at);

CREATE TABLE IF NOT EXISTS consumed_nonces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL,
    nonce_hash TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(subject_id, nonce_hash)
);
CREATE INDEX IF NOT EXISTS idx_consumed_nonces_expiry ON consumed_nonces(expires_at);

CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id TEXT NOT NULL,
    route_key TEXT NOT NULL,
    window_start REAL NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(subject_id, route_key, window_start)
);
CREATE INDEX IF NOT EXISTS idx_rate_limit_buckets_window ON rate_limit_buckets(window_start);
