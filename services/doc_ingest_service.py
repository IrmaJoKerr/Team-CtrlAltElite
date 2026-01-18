from typing import List, Optional
import logging
from datetime import datetime, timezone

import services.storage_service as storage_service
import services.embedding_service as embedding_service
import services.db_service as db_service
from services.contracts import IngestResult

LOG = logging.getLogger(__name__)


def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 100) -> List[str]:
    """Simple deterministic chunking used by the ingest pipeline.

    Kept intentionally simple and deterministic for unit testing.
    """
    chunks = []
    current_start = 0
    if not text:
        return []
    while current_start < len(text):
        chunk = text[current_start : current_start + chunk_size]
        chunks.append(chunk)
        current_start += chunk_size - overlap
        if current_start < 0:
            current_start = 0
    return chunks


async def ingest_from_gs_event(bucket: str, object_name: str) -> IngestResult:
    """Minimal ingestion orchestration.

    This function is intentionally conservative: it delegates text extraction
    to `services.storage_service.extract_text_from_pdf_gs_uri` and embeddings
    to `services.embedding_service.get_text_embeddings`. It currently does not
    perform DB upserts; that will be added in a following change.

    Returns an `IngestResult` with a best-effort status.
    """
    gs_uri = f"gs://{bucket}/{object_name}" if bucket else object_name
    try:
        text = storage_service.extract_text_from_pdf_gs_uri(gs_uri)
    except Exception as e:
        LOG.exception("Failed to extract text for %s/%s: %s", bucket, object_name, e)
        raise

    chunks = chunk_text(text)
    if not chunks:
        return IngestResult(
            document_id=str(object_name), chunks_created=0, status="no_text"
        )

    # Generate embeddings using the embedding service
    try:
        embeddings = await embedding_service.get_text_embeddings(chunks)
    except Exception as e:
        LOG.exception(
            "Embedding generation failed for %s/%s: %s", bucket, object_name, e
        )
        # Return partial result without raising to allow caller to decide retry semantics
        return IngestResult(
            document_id=str(object_name),
            chunks_created=len(chunks),
            status="embeddings_failed",
        )

    # Basic sanity check
    if len(embeddings) != len(chunks):
        LOG.error(
            "Mismatch between chunks and embeddings: %d vs %d",
            len(chunks),
            len(embeddings),
        )
        return IngestResult(
            document_id=str(object_name),
            chunks_created=len(chunks),
            status="embeddings_mismatch",
        )

    # Build DB records and upsert using db_service helper
    records = []
    for idx, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        rec = {
            "original_gcs_filename": object_name,
            "gcs_object_path": gs_uri,
            "department_folder": bucket or None,
            "chunk_index": idx,
            "chunk_content": chunk,
            "final_title": object_name,
            "final_department": bucket or None,
            "final_process_type": "ingest",
            "final_status": "confirmed",
            "review_status": "approved",
            "upload_session_id": None,
            "embedding_vector": emb,
        }
        records.append(rec)

    try:
        inserted = db_service.upsert_chunk_records(records)
        LOG.info("Upserted %d chunk records for %s", inserted, object_name)
    except Exception as e:
        LOG.exception("DB upsert failed for %s/%s: %s", bucket, object_name, e)
        return IngestResult(
            document_id=str(object_name),
            chunks_created=len(chunks),
            status="db_upsert_failed",
        )

    return IngestResult(
        document_id=str(object_name), chunks_created=len(chunks), status="ok"
    )
