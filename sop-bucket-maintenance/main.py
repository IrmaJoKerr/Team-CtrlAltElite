from datetime import datetime, timezone, timedelta
import os
from fastapi import FastAPI
from google.cloud import storage

app = FastAPI()

BUCKET_NAME = os.environ.get("BUCKET_NAME", "sop-originals-bucket-ctrlaltelite")
THRESHOLD_HOURS = int(os.environ.get("THRESHOLD_HOURS", "24"))


def newest_blob_time(bucket: storage.Bucket):
    blobs = list(bucket.list_blobs())
    if not blobs:
        return None
    newest = max((b.updated for b in blobs if b.updated is not None), default=None)
    return newest


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/flush-if-idle")
def flush_if_idle():
    client = storage.Client()
    bucket = client.bucket(BUCKET_NAME)

    newest = newest_blob_time(bucket)
    if newest is None:
        return {"flushed": False, "deleted_count": 0, "reason": "bucket empty"}

    now = datetime.now(timezone.utc)
    threshold = now - timedelta(hours=THRESHOLD_HOURS)

    if newest >= threshold:
        return {"flushed": False, "deleted_count": 0, "reason": "recent upload within threshold"}

    # newest < threshold -> delete all objects
    deleted = 0
    for blob in bucket.list_blobs():
        try:
            blob.delete()
            deleted += 1
        except Exception:
            # continue deleting others even if one fails
            continue

    return {"flushed": True, "deleted_count": deleted, "reason": f"no uploads for {THRESHOLD_HOURS} hours"}
