-- Persist the credential identifier returned at registration.
-- Legacy rows use a deterministic identifier derived from the already-unique device_id.
CREATE TABLE device_credentials_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    credential_id TEXT NOT NULL,
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

INSERT INTO device_credentials_v2 (
    id, device_id, credential_id, device_name, platform, credential_hash, status,
    expires_at, last_seen_at, revoked_at, revoke_reason, created_at, updated_at
)
SELECT
    id, device_id, 'cred-' || device_id, device_name, platform, credential_hash, status,
    expires_at, last_seen_at, revoked_at, revoke_reason, created_at, updated_at
FROM device_credentials;

DROP TABLE device_credentials;
ALTER TABLE device_credentials_v2 RENAME TO device_credentials;
CREATE UNIQUE INDEX idx_device_credentials_device_id ON device_credentials(device_id);
CREATE UNIQUE INDEX idx_device_credentials_credential_id ON device_credentials(credential_id);
CREATE INDEX idx_device_credentials_status ON device_credentials(status, revoked_at);
