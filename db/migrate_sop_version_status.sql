-- Migration: SOP Version Activation & Status
-- Purpose: Enforce "ACTIVE versions only" constraint for queries
-- Run after: migrate_query_audit.sql

-- 1. Extend sop_versions with activation status
ALTER TABLE sop_versions 
ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'draft',
ADD COLUMN IF NOT EXISTS activated_at TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS activated_by TEXT,
ADD COLUMN IF NOT EXISTS superseded_by_version_id INT,
ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS is_queryable BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS queryable_from TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS queryable_until TIMESTAMP WITH TIME ZONE;

-- Add constraint for valid status values
DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sop_versions_status_check') THEN
        ALTER TABLE sop_versions 
        ADD CONSTRAINT sop_versions_status_check 
        CHECK (status IN ('draft', 'pending_review', 'active', 'archived', 'superseded'));
    END IF;
END $$;

-- 2. Add SOP-level metadata to sops table (if not exists)
ALTER TABLE sops
ADD COLUMN IF NOT EXISTS current_active_version_id INT,
ADD COLUMN IF NOT EXISTS sop_code VARCHAR(50),  -- e.g. "SOP-HR-001"
ADD COLUMN IF NOT EXISTS title TEXT,
ADD COLUMN IF NOT EXISTS description TEXT,
ADD COLUMN IF NOT EXISTS category TEXT,
ADD COLUMN IF NOT EXISTS last_reviewed_at TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS next_review_due TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS is_critical BOOLEAN DEFAULT FALSE;

-- 3. Create view for ACTIVE queryable versions only
CREATE OR REPLACE VIEW active_sop_versions AS
SELECT 
    sv.*,
    s.sop_code,
    s.title as sop_title,
    s.department,
    s.category
FROM sop_versions sv
JOIN sops s ON sv.sop_id = s.sop_id
WHERE sv.status = 'active'
  AND sv.is_queryable = TRUE
  AND (sv.queryable_from IS NULL OR sv.queryable_from <= NOW())
  AND (sv.queryable_until IS NULL OR sv.queryable_until > NOW());

-- 4. Chunk-to-SOP Version mapping (links embeddings to specific SOP versions)
CREATE TABLE IF NOT EXISTS chunk_sop_mapping (
    id SERIAL PRIMARY KEY,
    chunk_id INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    sop_id INT NOT NULL REFERENCES sops(sop_id),
    sop_version_id INT NOT NULL REFERENCES sop_versions(version_id),
    section_number VARCHAR(50),     -- e.g. "3.1.2" for document structure
    section_title TEXT,
    step_index INT,                 -- NULL if not a step, else procedural order
    is_procedural_step BOOLEAN DEFAULT FALSE,
    step_text TEXT,                 -- Extracted step text if procedural
    chunk_order INT,                -- Order within the SOP version
    extracted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(chunk_id, sop_version_id)  -- Each chunk maps to one version
);

CREATE INDEX IF NOT EXISTS idx_chunk_sop_mapping_chunk ON chunk_sop_mapping(chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunk_sop_mapping_version ON chunk_sop_mapping(sop_version_id);
CREATE INDEX IF NOT EXISTS idx_chunk_sop_mapping_sop ON chunk_sop_mapping(sop_id);
CREATE INDEX IF NOT EXISTS idx_chunk_sop_mapping_procedural ON chunk_sop_mapping(is_procedural_step) WHERE is_procedural_step = TRUE;

-- 5. Function to activate a version (atomically supersede old, activate new)
CREATE OR REPLACE FUNCTION activate_sop_version(
    p_sop_id INT,
    p_version_id INT,
    p_activated_by TEXT
) RETURNS VOID AS $$
DECLARE
    v_current_active INT;
BEGIN
    -- Get current active version
    SELECT current_active_version_id INTO v_current_active 
    FROM sops WHERE sop_id = p_sop_id;
    
    -- Supersede old version if exists
    IF v_current_active IS NOT NULL AND v_current_active != p_version_id THEN
        UPDATE sop_versions 
        SET status = 'superseded',
            superseded_by_version_id = p_version_id,
            superseded_at = NOW(),
            is_queryable = FALSE
        WHERE version_id = v_current_active;
    END IF;
    
    -- Activate new version
    UPDATE sop_versions 
    SET status = 'active',
        activated_at = NOW(),
        activated_by = p_activated_by,
        is_queryable = TRUE,
        queryable_from = NOW()
    WHERE version_id = p_version_id;
    
    -- Update SOP pointer
    UPDATE sops 
    SET current_active_version_id = p_version_id,
        last_reviewed_at = NOW()
    WHERE sop_id = p_sop_id;
END;
$$ LANGUAGE plpgsql;

-- 6. Example: Mark existing versions as active (run once to bootstrap)
-- UPDATE sop_versions SET status = 'active', is_queryable = TRUE, activated_at = NOW() WHERE status = 'draft';
