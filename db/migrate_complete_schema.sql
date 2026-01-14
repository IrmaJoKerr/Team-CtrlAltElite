-- Combined Schema Migration for DocIntel System
-- Run this to set up the complete database schema from scratch
-- Date: 2026-01-14

-- ============================================================================
-- PART 1: Enable Extensions
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================================
-- PART 2: Core Tables (from migrate_add_versioning_and_audit.sql)
-- ============================================================================

-- SOP Documents master table
CREATE TABLE IF NOT EXISTS sop_documents (
  id SERIAL PRIMARY KEY,
  original_gcs_filename TEXT UNIQUE,
  created_at TIMESTAMP DEFAULT now()
);

-- SOP Versions for version history
CREATE TABLE IF NOT EXISTS sop_versions (
  id SERIAL PRIMARY KEY,
  document_id INT REFERENCES sop_documents(id),
  version INT NOT NULL,
  payload JSONB,
  created_by TEXT,
  created_at TIMESTAMP DEFAULT now()
);

-- Audit log for tracking changes
CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY,
  document_id INT,
  field TEXT,
  old_value TEXT,
  new_value TEXT,
  username TEXT,
  ts TIMESTAMP DEFAULT now()
);

-- Overrides table (simple)
CREATE TABLE IF NOT EXISTS overrides (
  id SERIAL PRIMARY KEY,
  document_id INT,
  username TEXT,
  justification TEXT,
  created_at TIMESTAMP DEFAULT now()
);

-- Users table for API-key based auth
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL,
  departments JSONB DEFAULT '[]',
  api_key TEXT UNIQUE,
  created_at TIMESTAMP DEFAULT now()
);

-- ============================================================================
-- PART 3: Documents Table (main chunk storage with embeddings)
-- ============================================================================

CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    original_gcs_filename TEXT,
    gcs_object_path TEXT,
    department_folder TEXT,
    chunk_index INT,
    chunk_content TEXT,
    embedding_vector vector(768),
    suggested_title TEXT,
    title_justification TEXT,
    suggested_department TEXT,
    department_justification TEXT,
    suggested_process_type TEXT,
    process_type_justification TEXT,
    suggested_status TEXT,
    status_justification TEXT,
    final_title TEXT,
    final_department TEXT,
    final_process_type TEXT,
    final_status TEXT,
    review_status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now(),
    -- Embedding pipeline fields
    document_type TEXT DEFAULT 'sop' CHECK (document_type IN ('sop', 'override')),
    embedding_status TEXT DEFAULT 'pending' CHECK (embedding_status IN ('pending', 'complete', 'failed')),
    embedding_eligible_at TIMESTAMP DEFAULT NOW(),
    embedding_attempt_count INT DEFAULT 0,
    embedding_last_attempt_at TIMESTAMP,
    embedding_error_message TEXT
);

-- Index for vector similarity search
CREATE INDEX IF NOT EXISTS documents_embedding_vector_idx 
ON documents USING ivfflat (embedding_vector vector_cosine_ops)
WITH (lists = 100);

-- Index for embedding worker
CREATE INDEX IF NOT EXISTS idx_documents_embedding_pending 
ON documents (embedding_status, embedding_eligible_at) 
WHERE embedding_status = 'pending';

-- Index for status queries
CREATE INDEX IF NOT EXISTS idx_documents_type_status 
ON documents (document_type, embedding_status);

-- ============================================================================
-- PART 4: Override Log Table (from migrate_precedent_system.sql)
-- ============================================================================

CREATE TABLE IF NOT EXISTS override_log (
    override_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id INT REFERENCES users(id),
    department TEXT,
    original_query TEXT,
    ai_response TEXT,
    user_override_justification TEXT,
    confidence_score DECIMAL(3,2),
    created_at TIMESTAMP DEFAULT now(),
    -- Resolution fields
    is_resolved BOOLEAN DEFAULT FALSE,
    resolution_notes TEXT,
    resolution_date TIMESTAMP,
    resolved_by_user_id INT REFERENCES users(id),
    -- Embedding pipeline fields
    embedding_status TEXT DEFAULT 'pending' CHECK (embedding_status IN ('pending', 'complete', 'failed')),
    embedding_eligible_at TIMESTAMP DEFAULT NOW(),
    embedding_attempt_count INT DEFAULT 0,
    embedding_last_attempt_at TIMESTAMP,
    embedding_error_message TEXT
);

-- Indexes for override_log
CREATE INDEX IF NOT EXISTS idx_override_log_resolved ON override_log(is_resolved);
CREATE INDEX IF NOT EXISTS idx_override_log_justification_trgm ON override_log USING gin(user_override_justification gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_override_log_resolution_date ON override_log(resolution_date) WHERE is_resolved = TRUE;
CREATE INDEX IF NOT EXISTS idx_override_log_department ON override_log(department);
CREATE INDEX IF NOT EXISTS idx_override_log_embedding_pending ON override_log (embedding_status, embedding_eligible_at) WHERE embedding_status = 'pending';

-- ============================================================================
-- PART 5: Upload Session Tables (from migrate_upload_sessions.sql)
-- ============================================================================

CREATE TABLE IF NOT EXISTS document_uploads (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id INT REFERENCES users(id),
    original_filename TEXT NOT NULL,
    gcs_temp_path TEXT,
    gcs_final_path TEXT,
    upload_status TEXT DEFAULT 'pending' CHECK (upload_status IN ('pending', 'processing', 'ready_for_review', 'confirmed', 'failed')),
    document_id INT,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS upload_metadata_drafts (
    id SERIAL PRIMARY KEY,
    session_id UUID REFERENCES document_uploads(session_id) ON DELETE CASCADE,
    -- AI-suggested fields
    suggested_title TEXT,
    title_confidence DECIMAL(3,2),
    suggested_department TEXT,
    department_confidence DECIMAL(3,2),
    suggested_process_type TEXT,
    process_type_confidence DECIMAL(3,2),
    suggested_status TEXT,
    status_confidence DECIMAL(3,2),
    -- User-edited fields (NULL until user edits)
    user_title TEXT,
    user_department TEXT,
    user_process_type TEXT,
    user_status TEXT,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS upload_audit_log (
    id SERIAL PRIMARY KEY,
    session_id UUID REFERENCES document_uploads(session_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    metadata_snapshot JSONB,
    user_id INT REFERENCES users(id),
    created_at TIMESTAMP DEFAULT now()
);

-- ============================================================================
-- PART 6: Helper Functions
-- ============================================================================

-- Function to get similar precedents using trigram similarity
CREATE OR REPLACE FUNCTION get_similar_precedents(
    search_text TEXT,
    similarity_threshold DECIMAL DEFAULT 0.3,
    limit_count INT DEFAULT 5,
    min_precedents INT DEFAULT 2
)
RETURNS TABLE (
    override_id UUID,
    department TEXT,
    original_query TEXT,
    user_override_justification TEXT,
    resolution_notes TEXT,
    similarity_score DECIMAL,
    resolved_by_user_id INT,
    resolution_date TIMESTAMP
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        ol.override_id,
        ol.department,
        ol.original_query,
        ol.user_override_justification,
        ol.resolution_notes,
        similarity(ol.user_override_justification, search_text)::DECIMAL as similarity_score,
        ol.resolved_by_user_id,
        ol.resolution_date
    FROM override_log ol
    WHERE ol.is_resolved = TRUE
      AND similarity(ol.user_override_justification, search_text) >= similarity_threshold
    ORDER BY similarity_score DESC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql;

-- Function to mark override as resolved
CREATE OR REPLACE FUNCTION mark_override_resolved(
    p_override_id UUID,
    p_resolution_notes TEXT,
    p_resolved_by_user_id INT
)
RETURNS BOOLEAN AS $$
BEGIN
    UPDATE override_log
    SET is_resolved = TRUE,
        resolution_notes = p_resolution_notes,
        resolution_date = NOW(),
        resolved_by_user_id = p_resolved_by_user_id
    WHERE override_id = p_override_id
      AND is_resolved = FALSE;
    
    RETURN FOUND;
END;
$$ LANGUAGE plpgsql;

-- Function to get pending embeddings for background worker
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
    FOR UPDATE SKIP LOCKED;
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

-- Function to mark embedding as failed
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

-- Function to increment embedding attempt count
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
-- PART 7: Insert default test user for development
-- ============================================================================

INSERT INTO users (username, role, departments, api_key)
VALUES ('test-manager', 'manager', '["loans", "operations", "compliance"]', 'demo-token')
ON CONFLICT (username) DO NOTHING;

INSERT INTO users (username, role, departments, api_key)
VALUES ('test-officer', 'officer', '["loans"]', 'officer-token')
ON CONFLICT (username) DO NOTHING;

-- ============================================================================
-- DONE! Verify with: SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';
-- ============================================================================
