from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from uuid import uuid4
from pathlib import Path
import os
import mimetypes
import asyncio

app = FastAPI(title="DocIntel Ingestion Service")

STORAGE_ROOT = os.getenv("STORAGE_ROOT", "local_test_store")


@app.get("/", tags=["health"])
def health():
    return {"status": "ok"}


@app.post("/upload", tags=["ingest"] )
async def upload_file(file: UploadFile = File(...)):
    filename = Path(file.filename).name
    ext = Path(filename).suffix.lower()
    allowed = {".pdf", ".txt"}
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="Only PDF and TXT files are allowed")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    # use local-first storage adapter; import lazily to avoid optional cloud deps at module import
    from adapters.storage_adapter import get_storage_adapter

    adapter = get_storage_adapter()
    object_name = f"{uuid4().hex}_{filename}"
    content_type = file.content_type or mimetypes.guess_type(filename)[0]
    try:
        uri = await asyncio.to_thread(adapter.upload_bytes, object_name, data, content_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    return JSONResponse({
        "uri": uri,
        "object": object_name,
        "filename": filename,
    })
