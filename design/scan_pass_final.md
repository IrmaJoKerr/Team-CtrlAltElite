**Scan Pass: Final Repo Mapping**

Summary: completed repo-wide scans across `docintel-data-processor`, `adapters`, `services`, `scripts`, `docintel-ingestion-service`, `processing-service-app`, `db`, and `utils`. Tests green after conservative comment→test changes. Below are remaining legacy blocks (file + line ranges) and recommended canonical targets.

- **`docintel-data-processor/main.py` — inline/legacy blocks (urgent candidates to move):**
  - `upload` / create session + metadata
    - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L1330-L1340)
    - Action: keep using `services.db_service.create_upload_session_with_metadata` (already called); consolidate any remaining `INSERT INTO document_uploads` and `INSERT INTO upload_metadata_drafts` into that helper.

  - `upload` text extraction (upload-time)
    - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L1288-L1310)
    - Canonical: `services.storage_service.extract_text_from_bytes` — [services/storage_service.py](services/storage_service.py#L100)

  - `confirm upload` (save to storage + INSERT documents)
    - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L1528-L1588)
    - Canonical: `adapters.storage_adapter.upload_bytes` via `get_storage_adapter()` and `services.db_service.upsert_chunk_records` — [services/db_service.py](services/db_service.py#L60)

  - `discard` / soft-delete flow and storage deletion
    - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L1748-L1808)
    - Canonical: `services.storage_service.delete_object` and `services.db_service.discard_upload_session` (proposed helper)

  - `update upload metadata` (draft updates)
    - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L1454-L1498)
    - Action: move `UPDATE upload_metadata_drafts` logic into `services.db_service.update_upload_metadata(session_id, edits)`

  - `update document metadata` (human edits)
    - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L2697-L2840)
    - Canonical: `services.db_service.update_document_metadata(gcs_object_path, metadata_update)`; audit/version writes delegate to `services.storage_service` (`gcs_write_json`, `append_audit`)

  - `confirm document metadata` (write version JSON + audit + DB approve)
    - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L2820-L3068)
    - Canonical: `services.storage_service.gcs_write_json` / `append_audit` and `services.db_service.confirm_document_metadata`

  - `hard delete` (DELETE FROM documents)
    - Location: [docintel-data-processor/main.py](docintel-data-processor/main.py#L3111-L3123)
    - Canonical: `services.db_service.delete_document_and_assets(gcs_object_path)` — should be transactional and call `services.storage_service.delete_object`.

- **Other files:**
  - `processing-service-app/main.py` contains legitimate DB event-processing SQL; keep in that service (not a migration target).
  - `scripts/` (backfills, reindex, sync_outbox) intentionally contain direct DB work and adapter calls — leave as-is for offline work.
  - `adapters/*` are pluggable provider implementations and are canonical for provider-specific operations (storage, embeddings, generative, qdrant, secrets).

Status & next steps (conservative workflow):
- Completed: repository scans (8 passes), mapping file created, commented large legacy blocks in `docintel-data-processor/main.py` and ran tests — green.
- Recommended next actions (pick one):
  - Option A (final cleanup): remove commented legacy blocks in a single cleanup commit, run tests, push and open PR. (I can do this.)
  - Option B (iterative): implement `services.db_service` helpers suggested above (e.g., `update_upload_metadata`, `confirm_document_metadata`, `discard_upload_session`, `delete_document_and_assets`) and move logic, testing after each small change.

If you approve Option A or B, tell me which and I will proceed. If Option B, confirm I should implement the `services/db_service` helpers and where to place unit tests (I can add tests mirroring current controller behaviors).
