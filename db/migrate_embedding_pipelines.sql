-- Migration: Embedding Pipeline Status Tracking
-- Purpose: Add columns to support document-type aware embedding with delayed/immediate processing
-- Run after: migrate_add_embedding_vector.sql
-- Date: 2026-01-14

-- ============================================================================
-- PART 1: Add embedding pipeline columns to documents table
-- ============================================================================

-- Document type: 'sop' (2-hour delay) vs 'override' (immediate embedding)
ALTER TABLE documents 
ADD COLUMN IF NOT EXISTS document_type TEXT DEFAULT 'sop' 
CHECK (document_type IN ('sop', 'override'));

-- Embedding status: tracks processing state
-- 'pending' = awaiting embedding generation
-- 'complete' = embedding successfully generated and stored
-- 'failed' = embedding failed after retry (needs admin intervention)
ALTER TABLE documents 
ADD COLUMN IF NOT EXISTS embedding_status TEXT DEFAULT 'pending'
CHECK (embedding_status IN ('pending', 'complete', 'failed'));

-- Timestamp when document becomes eligible for embedding
-- For SOPs: created_at + 2 hours (edit window)
-- For overrides: created_at (immediate)
ALTER TABLE documents 
ADD COLUMN IF NOT EXISTS embedding_eligible_at TIMESTAMP DEFAULT NOW();

-- Track retry attempts (max 1 retry = 2 total attempts)
ALTER TABLE documents 
ADD COLUMN IF NOT EXISTS embedding_attempt_count INT DEFAULT 0;

-- Timestamp of last embedding attempt (for debugging/monitoring)
ALTER TABLE documents 
ADD COLUMN IF NOT EXISTS embedding_last_attempt_at TIMESTAMP;

-- Error message from last failed attempt (for admin review)
ALTER TABLE documents 
ADD COLUMN IF NOT EXISTS embedding_error_message TEXT;

-- ============================================================================
-- PART 2: Add same columns to override_log for override justifications
-- ============================================================================

-- Override justifications need their own embedding tracking
ALTER TABLE override_log
ADD COLUMN IF NOT EXISTS embedding_status TEXT DEFAULT 'pending'
CHECK (embedding_status IN ('pending', 'complete', 'failed'));

ALTER TABLE override_log
ADD COLUMN IF NOT EXISTS embedding_eligible_at TIMESTAMP DEFAULT NOW();

ALTER TABLE override_log
ADD COLUMN IF NOT EXISTS embedding_attempt_count INT DEFAULT 0;

ALTER TABLE override_log
ADD COLUMN IF NOT EXISTS embedding_last_attempt_at TIMESTAMP;

ALTER TABLE override_log
ADD COLUMN IF NOT EXISTS embedding_error_message TEXT;

-- Override justifications always embed immediately (no delay)
-- So embedding_eligible_at defaults to NOW()

-- ============================================================================
-- PART 3: Create indexes for efficient polling
-- ============================================================================

-- Index for background worker: find pending documents ready for embedding
CREATE INDEX IF NOT EXISTS idx_documents_embedding_pending 
ON documents (embedding_status, embedding_eligible_at) 
WHERE embedding_status = 'pending';

-- Index for status queries by document type
CREATE INDEX IF NOT EXISTS idx_documents_type_status 
ON documents (document_type, embedding_status);

-- Index for override_log embedding status
CREATE INDEX IF NOT EXISTS idx_override_log_embedding_pending
ON override_log (embedding_status, embedding_eligible_at)
WHERE embedding_status = 'pending';

-- ============================================================================
-- PART 4: Backfill existing data
-- ============================================================================

-- Mark existing documents with embeddings as 'complete'
UPDATE documents 
SET embedding_status = 'complete',
    document_type = 'sop'
WHERE embedding_vector IS NOT NULL 
  AND embedding_status = 'pending';

-- Mark existing documents without embeddings as 'pending' (will be picked up by worker)
UPDATE documents 
SET embedding_status = 'pending',
    embedding_eligible_at = NOW(),
    document_type = 'sop'
WHERE embedding_vector IS NULL 
  AND embedding_status = 'pending';

-- Mark existing resolved overrides as 'complete' (assume they were processed)
UPDATE override_log
SET embedding_status = 'complete'
WHERE is_resolved = TRUE
  AND embedding_status = 'pending';

-- ============================================================================
-- PART 5: Create helper function for background worker
-- ============================================================================

-- Function to get next batch of documents ready for embedding
CREATE OR REPLACE FUNCTION get_pending_embeddings(batch_limit INT DEFAULT 10)
RETURNS TABLE (
    doc_id INT,
    doc_type TEXT,
    chunk_content TEXT,
    gcs_object_path TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        d.id,
        d.document_type,
        d.chunk_content,
        d.gcs_object_path
    FROM documents d
    WHERE d.embedding_status = 'pending'
      AND d.embedding_eligible_at <= NOW()
      AND d.embedding_attempt_count < 2
    ORDER BY d.embedding_eligible_at ASC
    LIMIT batch_limit
    FOR UPDATE SKIP LOCKED;  -- Prevent race conditions in multi-worker scenario
END;
$$ LANGUAGE plpgsql;

-- Function to mark embedding as complete
CREATE OR REPLACE FUNCTION mark_embedding_complete(doc_id INT)
RETURNS VOID AS $$
BEGIN
    UPDATE documents
    SET embedding_status = 'complete',
        embedding_last_attempt_at = NOW()
    WHERE id = doc_id;
END;
$$ LANGUAGE plpgsql;

-- Function to mark embedding as failed (after retry exhausted)
CREATE OR REPLACE FUNCTION mark_embedding_failed(doc_id INT, error_msg TEXT)
RETURNS VOID AS $$
BEGIN
    UPDATE documents
    SET embedding_status = 'failed',
        embedding_error_message = error_msg,
        embedding_last_attempt_at = NOW()
    WHERE id = doc_id;
END;
$$ LANGUAGE plpgsql;

-- Function to increment attempt count before retry
CREATE OR REPLACE FUNCTION increment_embedding_attempt(doc_id INT)
RETURNS INT AS $$
DECLARE
    new_count INT;
BEGIN
    UPDATE documents
    SET embedding_attempt_count = embedding_attempt_count + 1,
        embedding_last_attempt_at = NOW()
    WHERE id = doc_id
    RETURNING embedding_attempt_count INTO new_count;
    
    RETURN new_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- VERIFICATION QUERIES (run manually to verify migration)
-- ============================================================================

-- Check column existence:
-- SELECT column_name, data_type, column_default 
-- FROM information_schema.columns 
-- WHERE table_name = 'documents' 
--   AND column_name IN ('document_type', 'embedding_status', 'embedding_eligible_at');

-- Check index existence:
-- SELECT indexname FROM pg_indexes WHERE tablename = 'documents';

-- Check pending documents:
-- SELECT COUNT(*) as pending_count FROM documents WHERE embedding_status = 'pending';

-- Check failed documents:
-- SELECT id, original_gcs_filename, embedding_error_message 
-- FROM documents WHERE embedding_status = 'failed';
