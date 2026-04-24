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
