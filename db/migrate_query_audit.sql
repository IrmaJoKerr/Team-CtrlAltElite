-- Migration: Query Audit & Session Tracking
-- Purpose: Full traceability for SOP Query Agent (audit-safe, replayable)
-- Run after: migrate_add_versioning_and_audit.sql

-- Enable UUID extension if not present
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Query Sessions: Every query is logged
CREATE TABLE IF NOT EXISTS query_sessions (
    session_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id INT REFERENCES users(id),
    user_role TEXT,
    user_departments JSONB,
    query_text TEXT NOT NULL,
    query_hash VARCHAR(64),  -- SHA256 hash for dedup detection
    query_timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    response_timestamp TIMESTAMP WITH TIME ZONE,
    status TEXT DEFAULT 'pending',  -- pending | success | partial_coverage | no_coverage | error
    confidence_score FLOAT,
    gaps_identified JSONB,  -- Array of coverage gaps
    ambiguities JSONB,      -- Array of ambiguities found
    human_judgment_required JSONB,  -- Fields requiring human review
    ip_address INET,
    user_agent TEXT
);

-- 2. Query Results: Each chunk retrieved for a query
CREATE TABLE IF NOT EXISTS query_results (
    result_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES query_sessions(session_id) ON DELETE CASCADE,
    sop_id INT,
    sop_version_id INT,
    chunk_id INT REFERENCES documents(id),
    chunk_content_hash VARCHAR(64),  -- For integrity verification
    relevance_score FLOAT,           -- Embedding similarity (0-1)
    was_used_in_answer BOOLEAN DEFAULT TRUE,
    citation_index INT,              -- Order in citations list
    retrieved_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. Query Audit Log: Events during query processing
CREATE TABLE IF NOT EXISTS query_audit_log (
    id SERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES query_sessions(session_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,  -- coverage_gap, speculation_avoided, version_mismatch, scope_violation, etc
    event_details JSONB,
    severity TEXT DEFAULT 'info',  -- info | warning | error
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 4. Response Cache: For replay and verification
CREATE TABLE IF NOT EXISTS query_response_cache (
    session_id UUID PRIMARY KEY REFERENCES query_sessions(session_id) ON DELETE CASCADE,
    summary_answer TEXT,
    steps JSONB,           -- Array of procedural steps if applicable
    full_response JSONB,   -- Complete structured response for replay
    model_used TEXT,       -- gemini-2.5-pro, etc
    model_version TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Indexes for query performance
CREATE INDEX IF NOT EXISTS idx_query_sessions_user ON query_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_query_sessions_timestamp ON query_sessions(query_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_query_sessions_status ON query_sessions(status);
CREATE INDEX IF NOT EXISTS idx_query_sessions_hash ON query_sessions(query_hash);
CREATE INDEX IF NOT EXISTS idx_query_results_session ON query_results(session_id);
CREATE INDEX IF NOT EXISTS idx_query_results_sop ON query_results(sop_id, sop_version_id);
CREATE INDEX IF NOT EXISTS idx_query_audit_session ON query_audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_query_audit_type ON query_audit_log(event_type);

-- Grant permissions (adjust role name as needed)
-- GRANT SELECT, INSERT ON query_sessions, query_results, query_audit_log, query_response_cache TO docintel_app;
