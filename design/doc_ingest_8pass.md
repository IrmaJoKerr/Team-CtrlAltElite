# Focused 8-pass analysis: extracting doc_ingest_service

This file contains the focused 8-pass cross-reference for extracting the ingestion pipeline from `docintel-data-processor/main.py` into `services/doc_ingest_service.py`.

Pass 1 — Surface scan
- Target: `/process-document` endpoint in `docintel-data-processor/main.py` (lines ~2000-2320).
- Goal: extract download/move, text extraction, chunking, embedding generation, DB upsert, and audit writes into `services/doc_ingest_service.py`.

Pass 2 — Static inventory
- `services/storage_service.py`: `extract_text_from_pdf_gs_uri`, `gcs_write_json`, `append_audit`, `write_json_path`, `list_versions`.
- `adapters/storage_adapter.py`: local adapter `read_bytes`, `move_object`, `list_objects`, `get_latest_timestamp`.
- `services/embedding_service.py`: `get_text_embeddings` async wrapper.
- `services/db_service.py`: `get_db_connection` (no upsert helper yet).
- `local_test_store/`: test fixtures used in TEST_MODE.

Pass 3 — Runtime interactions
- GCS operations: `main.py` uses `storage_client` to copy/delete blobs — prefer using `storage_service`.
- DB writes: `main.py` uses `execute_values` with a custom cast to `vector` for `embedding_vector`.
- Audits/versions: `main.py` writes versions via `storage_service.gcs_write_json` and `append_audit`.

Pass 4 — Tests
- Existing tests exercise storage and embedding helpers; add `tests/test_doc_ingest_service.py` for the new service.

Pass 5 — Error paths
- Preserve storage fallback to GCS client, embedding length mismatch checks, and DB transaction rollback behavior.

Pass 6 — Mocks for tests
- Mock `services.storage_service.extract_text_from_pdf_gs_uri` and `services.embedding_service.get_text_embeddings`.

Pass 7 — Service contract
- Provide `async def ingest_from_gs_event(bucket: str, object_name: str) -> IngestResult`.
- Export `chunk_text`.

Pass 8 — Rollout safety
- Implement new service file and tests, update `main.py` to delegate, comment out original code, run tests and smoke suite.

Generated: 2026-01-19
