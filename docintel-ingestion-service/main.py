from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from uuid import uuid4
from pathlib import Path
import os
import mimetypes
import asyncio
import logging
from pydantic import BaseModel
from typing import Optional, Dict, Any

from .ingestion_pipeline import run_ingest_pipeline

app = FastAPI(title="DocIntel Ingestion Service")

STORAGE_ROOT = os.getenv("STORAGE_ROOT", "local_test_store")


@app.get("/", tags=["health"])
def health():
    return {"status": "ok"}


@app.post("/upload", tags=["ingest"])
async def upload_file(file: UploadFile = File(...)):
    filename = Path(file.filename).name
    ext = Path(filename).suffix.lower()
    allowed = {".pdf", ".txt"}
    if ext not in allowed:
        raise HTTPException(
            status_code=400, detail="Only PDF and TXT files are allowed"
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    # use local-first storage adapter; import lazily to avoid optional cloud deps at module import
    from adapters.storage_adapter import get_storage_adapter

    adapter = get_storage_adapter()
    object_name = f"{uuid4().hex}_{filename}"
    content_type = file.content_type or mimetypes.guess_type(filename)[0]
    try:
        uri = await asyncio.to_thread(
            adapter.upload_bytes, object_name, data, content_type
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    # Optionally auto-trigger ingestion after successful upload (feature-flag)
    auto_ingest = os.getenv("AUTO_INGEST_ON_UPLOAD", "").lower() in ("1", "true", "yes")
    enqueue_outbox = os.getenv("ENQUEUE_OUTBOX_ON_UPLOAD", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if auto_ingest:
        try:
            # fire-and-forget pipeline; pass minimal session_meta
            asyncio.create_task(
                run_ingest_pipeline(
                    None,
                    object_name,
                    {"title": filename, "upload_session_id": None},
                    enqueue_outbox=enqueue_outbox,
                )
            )
        except Exception:
            # don't fail the upload if ingestion scheduling fails
            logging.exception("Failed scheduling ingestion pipeline")

    return JSONResponse(
        {
            "uri": uri,
            "object": object_name,
            "filename": filename,
        }
    )


class ConfirmPayload(BaseModel):
    object: str
    bucket: Optional[str] = None
    session_meta: Optional[Dict[str, Any]] = None


@app.post("/confirm-upload", tags=["ingest"])
async def confirm_upload(
    payload: ConfirmPayload, sync: bool = False, enqueue_outbox: bool = False
):
    """Confirm an upload and run the ingestion pipeline.

    - `sync=True` will wait for pipeline completion (useful for tests).
    - `enqueue_outbox=True` will attempt to run the outbox sync after upsert.
    """
    try:
        if sync:
            result = await run_ingest_pipeline(
                payload.bucket,
                payload.object,
                payload.session_meta or {},
                enqueue_outbox=enqueue_outbox,
            )
            return JSONResponse({"status": "accepted", "result": result})
        else:
            # fire-and-forget pipeline execution
            asyncio.create_task(
                run_ingest_pipeline(
                    payload.bucket,
                    payload.object,
                    payload.session_meta or {},
                    enqueue_outbox=enqueue_outbox,
                )
            )
            return JSONResponse({"status": "accepted", "object": payload.object})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ingestion pipeline error: {e}")
