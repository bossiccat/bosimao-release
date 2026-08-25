CREATE TABLE IF NOT EXISTS control_plane_sessions (
    session_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (state IN ('ACTIVE', 'TERMINATING', 'TERMINATED',
                         'TERMINATION_PARTIAL', 'TERMINATION_TIMEOUT', 'KWS_READY')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (session_id, generation)
);

CREATE TABLE IF NOT EXISTS control_plane_terminations (
    termination_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    operation TEXT NOT NULL
        CHECK (operation IN ('terminate', 'retry')),
    request_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    parent_termination_id TEXT,
    result TEXT NOT NULL DEFAULT 'pending'
        CHECK (result IN ('pending', 'complete', 'partial', 'timeout')),
    state TEXT NOT NULL DEFAULT 'TERMINATING'
        CHECK (state IN ('TERMINATING', 'TERMINATED',
                         'TERMINATION_PARTIAL', 'TERMINATION_TIMEOUT')),
    terminal_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES control_plane_sessions(session_id),
    FOREIGN KEY (parent_termination_id) REFERENCES control_plane_terminations(termination_id),
    /* request_id is unique per session+generation regardless of operation */
    UNIQUE (session_id, generation, request_id)
);

CREATE TABLE IF NOT EXISTS control_plane_acknowledgements (
    termination_id TEXT NOT NULL,
    acknowledgement TEXT NOT NULL
        CHECK (acknowledgement IN (
            'android_trtc_left',
            'sidecar_trtc_left',
            'bridge_drained_closed',
            'apm_cancelled_closed',
            'brain_turns_sealed'
        )),
    result TEXT NOT NULL
        CHECK (result IN ('pending', 'confirmed', 'failed')),
    inherited INTEGER NOT NULL DEFAULT 0 CHECK (inherited IN (0, 1)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (termination_id, acknowledgement),
    FOREIGN KEY (termination_id) REFERENCES control_plane_terminations(termination_id) ON DELETE CASCADE
);

-- Raw reporter-bound acknowledgement reports (P1-3).
-- bridge_drained_closed is the aggregate of sidecar + rtc_bridge reports:
-- both reporters must confirm before the aggregate ack is confirmed.
CREATE TABLE IF NOT EXISTS control_plane_ack_reports (
    termination_id TEXT NOT NULL,
    acknowledgement TEXT NOT NULL,
    reporter TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('confirmed', 'failed')),
    error_code TEXT,
    reported_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (termination_id, acknowledgement, reporter),
    FOREIGN KEY (termination_id) REFERENCES control_plane_terminations(termination_id) ON DELETE CASCADE
);

-- KWS readiness evidence reported by Android (P1-5).
CREATE TABLE IF NOT EXISTS control_plane_kws_readiness (
    session_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    reporter TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    recorded_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, generation, reporter),
    FOREIGN KEY (session_id) REFERENCES control_plane_sessions(session_id)
);
