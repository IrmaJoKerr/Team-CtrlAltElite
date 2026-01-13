-- Migration: add versioning, audit log, overrides, and users table
CREATE TABLE IF NOT EXISTS sop_documents (
  id SERIAL PRIMARY KEY,
  original_gcs_filename TEXT UNIQUE,
  created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sop_versions (
  id SERIAL PRIMARY KEY,
  document_id INT REFERENCES sop_documents(id),
  version INT NOT NULL,
  payload JSONB,
  created_by TEXT,
  created_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY,
  document_id INT,
  field TEXT,
  old_value TEXT,
  new_value TEXT,
  username TEXT,
  ts TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS overrides (
  id SERIAL PRIMARY KEY,
  document_id INT,
  username TEXT,
  justification TEXT,
  created_at TIMESTAMP DEFAULT now()
);

-- Minimal users table for API-key based auth (hackathon MVP)
CREATE TABLE IF NOT EXISTS users (
  id SERIAL PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  role TEXT NOT NULL,
  departments JSONB DEFAULT '[]',
  api_key TEXT UNIQUE,
  created_at TIMESTAMP DEFAULT now()
);
