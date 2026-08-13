-- Split pending discovery from one-time authorization consumption.
ALTER TABLE pending_session_claims ADD COLUMN claim_token_hash TEXT;
ALTER TABLE pending_session_claims ADD COLUMN claimed_at REAL;
ALTER TABLE pending_session_claims ADD COLUMN signed_at REAL;
CREATE UNIQUE INDEX idx_pending_session_claims_token ON pending_session_claims(claim_token_hash);
