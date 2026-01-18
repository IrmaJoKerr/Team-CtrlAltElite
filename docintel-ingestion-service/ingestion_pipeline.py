import asyncio
import logging
from typing import Any, Dict, Optional

import services.doc_ingest_service as doc_ingest_service
import services.storage_service as storage_service
import services.embedding_service as embedding_service
import services.db_service as db_service
from services.sync_outbox_service import SyncOutboxService

LOG = logging.getLogger(__name__)


async def run_ingest_pipeline(
    bucket: Optional[str],
    object_name: str,
    session_meta: Optional[Dict[str, Any]] = None,
    enqueue_outbox: bool = False,
) -> Dict[str, Any]:
    """Run the full ingestion pipeline for a single object.

    Steps:
    - extract text via `services.storage_service`
    - chunk text with canonical chunker
    - compute embeddings via `services.embedding_service`
    - upsert chunk records via `services.db_service.upsert_chunk_records`
    - append an audit entry
    - optionally trigger outbox sync

    Returns a status dict suitable for API responses and tests.
    """
    gs_uri = f"gs://{bucket}/{object_name}" if bucket else object_name

    # Extract text in a thread to avoid blocking
    try:
        text = await asyncio.to_thread(
            storage_service.extract_text_from_pdf_gs_uri, gs_uri
        )
    except Exception as e:
        LOG.exception("Extraction failed for %s: %s", gs_uri, e)
        return {"status": "extraction_failed", "error": str(e)}

    chunks = doc_ingest_service.chunk_text(text)
    if not chunks:
        return {"status": "no_text", "chunks": 0}

    # embeddings is an async API
    try:
        embeddings = await embedding_service.get_text_embeddings(chunks)
    except Exception as e:
        LOG.exception("Embedding generation failed for %s: %s", gs_uri, e)
        return {"status": "embeddings_failed", "chunks": len(chunks), "error": str(e)}

    if len(embeddings) != len(chunks):
        LOG.error(
            "Embedding count mismatch for %s: %d vs %d",
            gs_uri,
            len(embeddings),
            len(chunks),
        )
        return {"status": "embeddings_mismatch", "chunks": len(chunks)}

    # Build DB records
    records = []
    for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        rec = {
            "original_gcs_filename": object_name,
            "gcs_object_path": gs_uri,
            "department_folder": bucket or None,
            "chunk_index": idx,
            "chunk_content": chunk,
            "final_title": session_meta.get("title") if session_meta else object_name,
            "final_department": (
                session_meta.get("department") if session_meta else (bucket or None)
            ),
            "final_process_type": "ingest",
            "final_status": "confirmed",
            "review_status": "approved",
            "upload_session_id": (
                session_meta.get("upload_session_id") if session_meta else None
            ),
            "embedding_vector": emb,
        }
        records.append(rec)

    # Upsert in a thread
    try:
        inserted = await asyncio.to_thread(db_service.upsert_chunk_records, records)
    except Exception as e:
        LOG.exception("DB upsert failed for %s: %s", gs_uri, e)
        return {"status": "db_upsert_failed", "chunks": len(chunks), "error": str(e)}

    # Append audit entry (fire-and-forget but run in thread to ensure persistence)
    try:
        audit_entry = {
            "event": "ingest_complete",
            "object": object_name,
            "chunks": len(chunks),
        }
        await asyncio.to_thread(
            storage_service.append_audit,
            bucket or "",
            object_name.rsplit("/", 1)[0] if "/" in object_name else object_name,
            audit_entry,
        )
    except Exception:
        LOG.exception("Failed to append audit for %s", gs_uri)

    # Optionally trigger outbox sync to push embeddings to Qdrant
    if enqueue_outbox:
        try:
            # Build sync service from env config; allow it to run one iteration
            qcoll = None
            qurl = None
            try:
                import os

                qcoll = os.environ.get("QDRANT_COLLECTION")
                qurl = os.environ.get("QDRANT_URL")
            except Exception:
                pass

            if qcoll and qurl:

                def _run_sync():
                    conn = db_service.get_db_connection()
                    try:
                        svc = SyncOutboxService(qcoll, qurl)
                        svc.run_once(conn, dry_run=False)
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass

                await asyncio.to_thread(_run_sync)
        except Exception:
            LOG.exception("Outbox sync failed for %s", gs_uri)

    return {"status": "ok", "chunks": len(chunks), "inserted": inserted}
