-- Run manually against existing PostgreSQL databases if tables were created before these columns existed.
-- Safe to run once; ignore errors if columns already exist.

ALTER TABLE milestones ADD COLUMN IF NOT EXISTS delivery_title VARCHAR(300);
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS delivery_external_links JSON DEFAULT '[]';
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS delivery_version_notes TEXT;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS invalid_delivery_reported BOOLEAN DEFAULT false;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS invalid_delivery_report_note TEXT;
ALTER TABLE milestones ADD COLUMN IF NOT EXISTS milestone_action_logs JSON DEFAULT '[]';

ALTER TABLE disputes ADD COLUMN IF NOT EXISTS milestone_id VARCHAR REFERENCES milestones(id) ON DELETE SET NULL;
ALTER TABLE disputes ADD COLUMN IF NOT EXISTS evidence_urls JSON DEFAULT '[]';

ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS high_value_checklist_threshold DOUBLE PRECISION;
ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS funding_deadline_days INTEGER NOT NULL DEFAULT 14;
ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS auto_release_days INTEGER NOT NULL DEFAULT 5;

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS initiated_by_user_id VARCHAR REFERENCES users(id);
UPDATE transactions SET initiated_by_user_id = buyer_id WHERE initiated_by_user_id IS NULL AND buyer_id IS NOT NULL;

ALTER TABLE wallet_transactions ADD COLUMN IF NOT EXISTS flagged_high_risk BOOLEAN DEFAULT false;

ALTER TABLE refunds ADD COLUMN IF NOT EXISTS milestone_id VARCHAR REFERENCES milestones(id) ON DELETE SET NULL;

ALTER TABLE transactions ADD COLUMN IF NOT EXISTS stale_activity_warn_sent_at TIMESTAMP;

ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS invite_expiry_days INTEGER NOT NULL DEFAULT 30;
ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS stale_activity_warn_days INTEGER NOT NULL DEFAULT 90;

CREATE TABLE IF NOT EXISTS agent_request_messages (
    id VARCHAR PRIMARY KEY,
    agent_request_id VARCHAR NOT NULL REFERENCES agent_requests(id) ON DELETE CASCADE,
    author_user_id VARCHAR NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    author_role VARCHAR(20) NOT NULL,
    body TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_agent_request_messages_req ON agent_request_messages(agent_request_id);

ALTER TABLE agent_earnings ADD COLUMN IF NOT EXISTS admin_payout_approved BOOLEAN DEFAULT false;
ALTER TABLE agent_earnings ADD COLUMN IF NOT EXISTS admin_payout_approved_at TIMESTAMP;
ALTER TABLE agent_earnings ADD COLUMN IF NOT EXISTS admin_payout_approved_by VARCHAR REFERENCES users(id);
