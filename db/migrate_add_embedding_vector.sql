-- Migration: add `embedding_vector` pgvector column and remove old JSON/text embedding column
-- Run this after enabling the `vector` extension (see create_pgvector_extension.sql)

ALTER TABLE IF EXISTS documents
  DROP COLUMN IF EXISTS embedding;

-- Add embedding_vector with an assumed dimension (adjust if your model uses a different dim)
ALTER TABLE IF EXISTS documents
  ADD COLUMN IF NOT EXISTS embedding_vector vector(1536);

-- Optional: create an ivfflat index for faster ANN queries (requires ANALYZE after insert)
CREATE INDEX IF NOT EXISTS documents_embedding_vector_idx
  ON documents USING ivfflat (embedding_vector vector_cosine_ops)
  WITH (lists = 100);
