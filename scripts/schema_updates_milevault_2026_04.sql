-- Run manually against existing PostgreSQL databases if tables were created before these columns existed.
-- Safe to run once; ignore errors if columns already exist.

-- Users: reputation + risk (required by app.models.user.User — fixes UndefinedColumn on Railway if DB is old)
ALTER TABLE users ADD COLUMN IF NOT EXISTS rating DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS rating_count DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS completion_rate DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS dispute_rate DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS total_volume DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS badges JSONB DEFAULT '[]'::jsonb;
ALTER TABLE users ADD COLUMN IF NOT EXISTS wallet_frozen BOOLEAN DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS withdrawals_blocked BOOLEAN DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS withdrawal_cooldown_until TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS risk_score DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verification_token VARCHAR(128);
ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verification_expires_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_users_email_verification_token ON users(email_verification_token);

ALTER TABLE milestones ADD COLUMN IF NOT EXISTS delivery_title VARCHAR(300);
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS delivery_external_links JSON DEFAULT '[]';
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS delivery_version_notes TEXT;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS invalid_delivery_reported BOOLEAN DEFAULT false;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS invalid_delivery_report_note TEXT;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS milestone_action_logs JSON DEFAULT '[]';
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS funded_amount DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS is_funded BOOLEAN DEFAULT false;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS funding_deadline TIMESTAMP;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS auto_release_at TIMESTAMP;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS delivery_note TEXT;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS delivery_attachments JSON DEFAULT '[]';
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS revision_note TEXT;

ALTER TABLE disputes ADD COLUMN IF NOT EXISTS milestone_id VARCHAR REFERENCES milestones(id) ON DELETE SET NULL;
ALTER TABLE disputes ADD COLUMN IF NOT EXISTS evidence_urls JSON DEFAULT '[]';

ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS high_value_checklist_threshold DOUBLE PRECISION;
ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS funding_deadline_days INTEGER NOT NULL DEFAULT 14;
ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS auto_release_days INTEGER NOT NULL DEFAULT 5;

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS initiated_by_user_id VARCHAR REFERENCES users(id);
UPDATE transactions SET initiated_by_user_id = buyer_id WHERE initiated_by_user_id IS NULL AND buyer_id IS NOT NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS funding_deadline TIMESTAMP;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS funded_milestones INTEGER DEFAULT 0;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS locked_exchange_rate DOUBLE PRECISION;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS locked_rate_from VARCHAR(10);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS locked_rate_to VARCHAR(10);

ALTER TABLE wallet_balances ADD COLUMN IF NOT EXISTS escrow_amount DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE wallet_balances ADD COLUMN IF NOT EXISTS pending_amount DOUBLE PRECISION DEFAULT 0.0;

ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS flagged_high_risk BOOLEAN DEFAULT false;
ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS milestone_id VARCHAR REFERENCES milestones(id) ON DELETE SET NULL;

ALTER TABLE refunds ADD COLUMN IF NOT EXISTS milestone_id VARCHAR REFERENCES milestones(id) ON DELETE SET NULL;

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS stale_activity_warn_sent_at TIMESTAMP;

ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS invite_expiry_days INTEGER NOT NULL DEFAULT 30;
ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS stale_activity_warn_days INTEGER NOT NULL DEFAULT 90;

-- FKs omitted so bootstrap succeeds on DBs where agent_requests/users exist but CREATE order differs.
CREATE TABLE IF NOT EXISTS agent_request_messages (
    id VARCHAR PRIMARY KEY,
    agent_request_id VARCHAR NOT NULL,
    author_user_id VARCHAR NOT NULL,
    author_role VARCHAR(20) NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_agent_request_messages_req ON agent_request_messages(agent_request_id);

ALTER TABLE agent_earnings ADD COLUMN IF NOT EXISTS admin_payout_approved BOOLEAN DEFAULT false;
ALTER TABLE agent_earnings ADD COLUMN IF NOT EXISTS admin_payout_approved_at TIMESTAMP;
ALTER TABLE agent_earnings ADD COLUMN IF NOT EXISTS admin_payout_approved_by VARCHAR REFERENCES users(id);
