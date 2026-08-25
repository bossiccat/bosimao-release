-- 方案 B 控制面：wake 事件原子消费（契约 §5 wake/re-enter）
-- 1) sessions 状态机扩展 SIGNING/ENTERING（wake 之后的新代从 SIGNING 开始）。
--    SQLite CHECK 不可 ALTER，按标准 12-step 流程重建表（数据量小，直接搬移）。
-- 2) wake_event_id 以 (device_id, prior_session_id, prior_generation, wake_event_id)
--    为作用域唯一；成功消费记录完整 payload 供幂等重放返回原结果。

CREATE TABLE control_plane_sessions_new (
    session_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (state IN ('ACTIVE', 'TERMINATING', 'TERMINATED',
                         'TERMINATION_PARTIAL', 'TERMINATION_TIMEOUT', 'KWS_READY',
                         'SIGNING', 'ENTERING')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (session_id, generation)
);

INSERT INTO control_plane_sessions_new (session_id, device_id, room_id, generation,
                                        state, created_at, updated_at)
SELECT session_id, device_id, room_id, generation, state, created_at, updated_at
FROM control_plane_sessions;

DROP TABLE control_plane_sessions;
ALTER TABLE control_plane_sessions_new RENAME TO control_plane_sessions;

CREATE TABLE IF NOT EXISTS control_plane_wake_events (
    wake_event_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    prior_session_id TEXT NOT NULL,
    prior_generation INTEGER NOT NULL,
    new_session_id TEXT NOT NULL,
    new_generation INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    record_json TEXT NOT NULL,
    consumed_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (device_id, prior_session_id, prior_generation, wake_event_id)
);
