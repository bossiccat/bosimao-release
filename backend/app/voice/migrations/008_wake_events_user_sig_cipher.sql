-- Task B：wake userSig 静态加密列（与 PG 迁移 cloudbase/migrations/
-- 20260908154838_voice_control_plane.sql:241-259 对齐）。
--
-- PG 侧两列是 NOT NULL：
--   user_sig_ciphertext         bytea NOT NULL   ← 这里用 BLOB 存同密度 bytes
--   user_sig_encryption_version text  NOT NULL   ← 这里用 TEXT 存版本号
-- consume_wake() 必须同事务写满两列，否则 PostgreSQL NOT NULL 约束直接拒绝写入。
--
-- DEFAULT x'' / '' 只为兼容本次迁移之前已存在的 wake 行（ALTER TABLE ADD COLUMN
-- 加 NOT NULL 必须有默认值）；新写入的行由应用层保证非空。遗留行 version 为空串，
-- 重放时 app/voice/control_plane_wake.py 会 fail-closed 抛
-- UserSigCiphertextMissing —— 遗留的明文 record_json 不得再被信任读出。

ALTER TABLE control_plane_wake_events
    ADD COLUMN user_sig_ciphertext BLOB NOT NULL DEFAULT x'';

ALTER TABLE control_plane_wake_events
    ADD COLUMN user_sig_encryption_version TEXT NOT NULL DEFAULT '';
