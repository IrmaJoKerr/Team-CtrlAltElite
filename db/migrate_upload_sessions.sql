-- Migration: Document Upload Sessions & Metadata Drafts
-- Purpose: Human-in-the-loop ingestion with edit/discard capability
-- Strict schema: only authorized fields, no extras

-- 1. Document Upload Sessions: Ephemeral state for each upload
CREATE TABLE IF NOT EXISTS document_uploads (
    session_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id INT NOT NULL REFERENCES users(id),
    filename TEXT NOT NULL,
    original_filename TEXT,
    upload_status TEXT NOT NULL DEFAULT 'draft', -- draft | pending_confirmation | confirmed | cancelled | discarded
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    confirmed_at TIMESTAMP WITH TIME ZONE,
    discarded_at TIMESTAMP WITH TIME ZONE,
    gcs_path TEXT,  -- Set when confirmed
    document_id INT REFERENCES documents(id),  -- Link to created document
    is_deleted BOOLEAN DEFAULT FALSE  -- Soft delete for grace period
);

-- 2. Upload Metadata Drafts: Pending edits before confirmation
CREATE TABLE IF NOT EXISTS upload_metadata_drafts (
    id SERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES document_uploads(session_id) ON DELETE CASCADE,
    extracted_title TEXT,           -- AI-extracted title
    extracted_department TEXT,      -- AI-extracted department
    extracted_author TEXT,          -- AI-extracted author
    extracted_type TEXT,            -- AI-extracted document type
    ai_confidence JSONB,            -- {title: 0.9, department: 0.8, ...}
    ai_model_used TEXT,             -- gemini-2.5-pro, etc
    user_title TEXT,                -- User-edited title (NULL if not changed)
    user_department TEXT,           -- User-edited department
    user_author TEXT,               -- User-edited author
    user_type TEXT,                 -- User-edited type
    confirmed_title TEXT,           -- Final title after confirmation
    confirmed_department TEXT,      -- Final department after confirmation
    confirmed_author TEXT,          -- Final author after confirmation
    confirmed_type TEXT,            -- Final type after confirmation
    last_edited_at TIMESTAMP WITH TIME ZONE,
    confirmed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. Upload Audit Log: Track all state transitions
CREATE TABLE IF NOT EXISTS upload_audit_log (
    id SERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES document_uploads(session_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,  -- extracted | edited | confirmed | discarded | error
    user_id INT REFERENCES users(id),
    field_changes JSONB,  -- {field_name: {from: old_value, to: new_value}, ...}
    event_details JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 4. Add is_deleted to documents table (if not exists)
ALTER TABLE documents
ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS upload_session_id UUID REFERENCES document_uploads(session_id);

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_document_uploads_user ON document_uploads(user_id);
CREATE INDEX IF NOT EXISTS idx_document_uploads_status ON document_uploads(upload_status);
CREATE INDEX IF NOT EXISTS idx_document_uploads_created ON document_uploads(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_upload_metadata_drafts_session ON upload_metadata_drafts(session_id);
CREATE INDEX IF NOT EXISTS idx_upload_audit_log_session ON upload_audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_documents_upload_session ON documents(upload_session_id);
CREATE INDEX IF NOT EXISTS idx_documents_is_deleted ON documents(is_deleted) WHERE is_deleted = TRUE;

-- Constraint: Only one non-discarded session per user-filename combo
-- (Enforced at application level for now)
