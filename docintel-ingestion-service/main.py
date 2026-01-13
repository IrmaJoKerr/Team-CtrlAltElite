from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import JSONResponse
from google.cloud import storage
from uuid import uuid4
from pathlib import Path
import os
import mimetypes

app = FastAPI(title="DocIntel Ingestion Service")

BUCKET_NAME = os.getenv("BUCKET_NAME", "ambuckethack")


def get_storage_client():
    return storage.Client()


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

    client = get_storage_client()
    try:
        bucket = client.bucket(BUCKET_NAME)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to access bucket: {e}")

    object_name = f"{uuid4().hex}_{filename}"
    blob = bucket.blob(object_name)
    content_type = file.content_type or mimetypes.guess_type(filename)[0]
    try:
        blob.upload_from_string(data, content_type=content_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    return JSONResponse({
        "gs_uri": f"gs://{BUCKET_NAME}/{object_name}",
        "object": object_name,
        "filename": filename,
    })
