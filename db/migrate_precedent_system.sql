-- Migration: Add Historical Precedent System to Override_Log
-- Purpose: Enable frontline staff to learn from similar resolved override cases
-- Version: 1.0
-- Date: 2026-01-14

-- ============================================================================
-- STEP 1: Enable pg_trgm extension (if not already enabled)
-- ============================================================================
-- Required for fuzzy text matching (similarity > 0.7)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- STEP 2: Extend Override_Log table with precedent fields
-- ============================================================================
-- These fields track whether an override was resolved and how

ALTER TABLE override_log
ADD COLUMN IF NOT EXISTS is_resolved BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS resolution_notes TEXT,
ADD COLUMN IF NOT EXISTS resolution_date TIMESTAMP WITH TIME ZONE,
ADD COLUMN IF NOT EXISTS resolved_by_user_id VARCHAR(255);

-- ============================================================================
-- STEP 3: Create indexes for precedent queries
-- ============================================================================
-- Index 1: Find resolved cases efficiently
CREATE INDEX IF NOT EXISTS idx_override_log_resolved_created
ON override_log(is_resolved, created_at DESC)
WHERE is_resolved = TRUE;

-- Index 2: Enable fuzzy text matching on override_reason
-- This allows similarity(override_reason, query_text) > 0.7 to use index
CREATE INDEX IF NOT EXISTS idx_override_log_reason_trgm
ON override_log USING GIST(override_reason gist_trgm_ops)
WHERE is_resolved = TRUE;

-- Index 3: Find precedents by resolution date (temporal queries)
CREATE INDEX IF NOT EXISTS idx_override_log_resolution_date
ON override_log(resolution_date DESC)
WHERE is_resolved = TRUE AND resolution_date IS NOT NULL;

-- Index 4: RBAC queries (show precedents by department)
CREATE INDEX IF NOT EXISTS idx_override_log_user_department
ON override_log(user_department, is_resolved)
WHERE is_resolved = TRUE;

-- ============================================================================
-- STEP 4: Add comments for clarity (documentation in DB)
-- ============================================================================
COMMENT ON COLUMN override_log.is_resolved IS 
  'TRUE if this override has been formally resolved and can serve as precedent for others. FALSE for open/unresolved cases.';

COMMENT ON COLUMN override_log.resolution_notes IS 
  'How was this override resolved? E.g., "Manager approved with Section 5.2 amendment", "Escalated to Compliance for SOP revision", etc. Provides context for similar cases.';

COMMENT ON COLUMN override_log.resolution_date IS 
  'When was the resolution finalized? Nullable until marked as resolved.';

COMMENT ON COLUMN override_log.resolved_by_user_id IS 
  'User ID of manager/compliance officer who marked this override as resolved. Provides accountability.';

-- ============================================================================
-- STEP 5: Create stored function for precedent retrieval
-- ============================================================================
-- This function safely retrieves similar precedents with 70% fuzzy match threshold
CREATE OR REPLACE FUNCTION get_similar_precedents(
  p_override_reason TEXT,
  p_current_query_id UUID,
  p_similarity_threshold FLOAT DEFAULT 0.70,
  p_limit_count INT DEFAULT 5,
  p_min_precedents INT DEFAULT 2
)
RETURNS TABLE(
  override_id UUID,
  user_id VARCHAR,
  user_department VARCHAR,
  original_recommendation TEXT,
  override_reason TEXT,
  resolution_notes TEXT,
  resolved_by_user_id VARCHAR,
  resolution_date TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE,
  match_score FLOAT,
  precedent_rank INT
) AS $$
BEGIN
  -- Safety check: if override_reason is null or empty, return early
  IF p_override_reason IS NULL OR p_override_reason = '' THEN
    RETURN;
  END IF;

  -- Core query: find similar resolved overrides using pg_trgm similarity
  RETURN QUERY
  SELECT 
    ol.override_id,
    ol.user_id,
    ol.user_department,
    ol.original_recommendation,
    ol.override_reason,
    ol.resolution_notes,
    ol.resolved_by_user_id,
    ol.resolution_date,
    ol.created_at,
    similarity(ol.override_reason, p_override_reason)::FLOAT AS match_score,
    ROW_NUMBER() OVER (ORDER BY similarity(ol.override_reason, p_override_reason) DESC, ol.resolution_date DESC)::INT AS precedent_rank
  FROM override_log ol
  WHERE 
    ol.is_resolved = TRUE
    AND ol.query_id != p_current_query_id
    AND similarity(ol.override_reason, p_override_reason) > p_similarity_threshold
    AND ol.override_reason IS NOT NULL
  ORDER BY 
    match_score DESC,
    ol.resolution_date DESC
  LIMIT p_limit_count;
END;
$$ LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE;

-- ============================================================================
-- STEP 6: Add helper function to mark override as resolved
-- ============================================================================
-- This is called when a manager/compliance officer formally resolves an override
CREATE OR REPLACE FUNCTION mark_override_resolved(
  p_override_id UUID,
  p_resolution_notes TEXT,
  p_resolved_by_user_id VARCHAR
)
RETURNS BOOLEAN AS $$
BEGIN
  UPDATE override_log
  SET 
    is_resolved = TRUE,
    resolution_notes = p_resolution_notes,
    resolution_date = NOW(),
    resolved_by_user_id = p_resolved_by_user_id
  WHERE override_id = p_override_id;
  
  -- Return true if update was successful
  RETURN FOUND;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- STEP 7: Validation check
-- ============================================================================
-- Ensure pg_trgm is enabled and indexes are created
SELECT 
  extname AS extension_name,
  extversion AS version
FROM pg_extension
WHERE extname = 'pg_trgm';

-- Verify indexes exist
SELECT 
  indexname,
  tablename,
  indexdef
FROM pg_indexes
WHERE tablename = 'override_log' 
  AND (indexname LIKE '%trgm%' OR indexname LIKE '%resolved%');

-- ============================================================================
-- ROLLBACK INSTRUCTIONS
-- ============================================================================
-- If rollback needed, run:
/*
DROP FUNCTION IF EXISTS get_similar_precedents(TEXT, UUID, FLOAT, INT, INT);
DROP FUNCTION IF EXISTS mark_override_resolved(UUID, TEXT, VARCHAR);
DROP INDEX IF EXISTS idx_override_log_resolved_created;
DROP INDEX IF EXISTS idx_override_log_reason_trgm;
DROP INDEX IF EXISTS idx_override_log_resolution_date;
DROP INDEX IF EXISTS idx_override_log_user_department;
ALTER TABLE override_log DROP COLUMN IF EXISTS is_resolved;
ALTER TABLE override_log DROP COLUMN IF EXISTS resolution_notes;
ALTER TABLE override_log DROP COLUMN IF EXISTS resolution_date;
ALTER TABLE override_log DROP COLUMN IF EXISTS resolved_by_user_id;
*/
