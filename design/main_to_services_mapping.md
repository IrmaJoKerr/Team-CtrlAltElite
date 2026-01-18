# Mapping: docintel-data-processor/main.py → canonical services/adapters

This file lists remaining legacy blocks in `docintel-data-processor/main.py` (start line ranges) and the canonical service/adapter that replaces each block.

- **Inline text extraction (upload-time)**
  - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L1288-L1310)
  - Description: Legacy PDF/text extraction performed inline in the upload handler (commented).
  - Canonical replacement: `services.storage_service.extract_text_from_bytes` — [services/storage_service.py](services/storage_service.py#L100)

- **Confirm upload: save to storage + INSERT into `documents` (simplified)**
  - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L1528-L1588)
  - Description: Builds a GCS path, simulated GCS save and a commented `INSERT INTO documents` legacy block.
  - Canonical replacements:
    - Persist file bytes: `adapters.storage_adapter.LocalStorageAdapter.upload_bytes` (via `adapters.storage_adapter.get_storage_adapter()`)
      — [adapters/storage_adapter.py](adapters/storage_adapter.py#L23)
    - Create/upsert chunk records: `services.db_service.upsert_chunk_records` — [services/db_service.py](services/db_service.py#L60)
    - Create upload session + metadata: `services.db_service.create_upload_session_with_metadata` — [services/db_service.py](services/db_service.py#L225)

- **Legacy GCS deletion (discard flow)**
  - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L1748-L1808)
  - Description: Commented block that deleted blobs via `storage_client`.
  - Canonical replacement: `services.storage_service.delete_object` — [services/storage_service.py](services/storage_service.py#L186)

- **Confirm document metadata → write version JSON + audit**
  - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L2820-L3068)
  - Description: Normal-mode branch writes a version JSON to GCS and appends an audit entry (with a test-mode local-path branch above it).
  - Canonical replacements:
    - Write version JSON: `services.storage_service.gcs_write_json` — [services/storage_service.py](services/storage_service.py#L12)
    - Append audit entry: `services.storage_service.append_audit` — [services/storage_service.py](services/storage_service.py#L155)
    - Local-mode helpers: `services.storage_service.write_json_path` / `read_json_path` — [services/storage_service.py](services/storage_service.py#L170)

- **Discard document endpoint (delete metadata / files)**
  - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L3038-L3074)
  - Description: Endpoint that deletes metadata and files (test-mode local deletes; normal-mode uses storage client).
  - Canonical replacement: `services.storage_service.delete_object` — [services/storage_service.py](services/storage_service.py#L186)

- **Audit / field-level change logging (write audit files)**
  - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L2768-L2788)
  - Description: Writes audit/version files to GCS/local store for field-level changes.
  - Canonical replacement: `services.storage_service.append_audit` / `gcs_write_json` — [services/storage_service.py](services/storage_service.py#L12) and [services/storage_service.py](services/storage_service.py#L155)

Notes and next steps
- Many `cursor.execute(...)` statements remain in `main.py`; these are a mix of small lookups/authorization checks and larger data writes. The larger data-write blocks were identified and delegated (see entries above). Remaining DB calls should be reviewed and either kept (if controller-level checks) or moved into `services/db_service.py` if they implement business logic or multi-statement transactions.
- `adapters/generative_adapter.generate_answer` is the canonical entry for generative responses; `main.py` delegates to it (see [adapters/generative_adapter.py](adapters/generative_adapter.py#L41)).
- `services/embedding_service.get_text_embeddings` is the canonical entry for embeddings — `main.py` and ingestion flows already call it.

If you approve this mapping, I can (per the conservative workflow) comment any remaining large `cursor.execute` blocks that should move to services, run the full test suite, and then remove them in a final cleanup commit.
