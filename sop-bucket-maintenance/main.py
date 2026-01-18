from datetime import datetime, timezone, timedelta
import os
import fcntl
import logging
from fastapi import FastAPI, Query
try:
    from google.cloud import storage
except Exception:
    storage = None

app = FastAPI()

BUCKET_NAME = os.environ.get("BUCKET_NAME", "sop-originals-bucket-ctrlaltelite")
THRESHOLD_HOURS = int(os.environ.get("THRESHOLD_HOURS", "24"))
LOCK_DIR = os.environ.get("LOCK_DIR") or os.path.join(os.environ.get("STORAGE_ROOT", "local_test_store"), ".locks")
QUARANTINE_PREFIX = os.environ.get("QUARANTINE_PREFIX", "quarantine")


def _acquire_lock(name: str):
    Path = __import__('pathlib').Path
    Path(LOCK_DIR).mkdir(parents=True, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, f"{name}.lock")
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        return None


def _release_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/flush-if-idle")
def flush_if_idle(simulate: bool = Query(False, description="If true do not perform destructive actions"),
                 confirm: bool = Query(False, description="If true perform quarantine deletes when allowed")):
    """Flush bucket documents if no recent uploads.

    Options:
    - `simulate`: do not perform writes/moves; only report what would happen.
    - `confirm`: actually move objects to quarantine (when not simulate). Without confirm, nothing is deleted.
    """
    # Acquire a short-lived lock to avoid concurrent workers
    lock_fd = _acquire_lock('sop-bucket-maintenance')
    if lock_fd is None:
        return {"flushed": False, "deleted_count": 0, "reason": "lock held by another process"}

    try:
        from adapters.storage_adapter import get_storage_adapter
        adapter = get_storage_adapter()
        # Ask adapter for latest timestamp for this prefix
        newest = adapter.get_latest_timestamp(BUCKET_NAME)
        if newest is None:
            return {"flushed": False, "deleted_count": 0, "reason": "bucket empty"}

        now = datetime.now(timezone.utc)
        threshold = now - timedelta(hours=THRESHOLD_HOURS)

        if newest >= threshold:
            return {"flushed": False, "deleted_count": 0, "reason": "recent upload within threshold"}

        # newest < threshold -> safe to quarantine/delete
        objs = adapter.list_objects(prefix=BUCKET_NAME)
        deleted = 0
        sample = None
        for obj in objs:
            if sample is None:
                sample = obj

            if simulate:
                # don't touch storage during simulation
                deleted += 1
                continue

            # perform quarantine move when confirmed, otherwise skip destructive action
            if confirm:
                try:
                    now_str = datetime.now(timezone.utc).isoformat().replace(':', '-')
                    new_name = f"{QUARANTINE_PREFIX}/{now_str}/{obj}"
                    ok = False
                    try:
                        ok = adapter.move_object(obj, new_name)
                    except Exception:
                        logging.exception("adapter.move_object failed for %s", obj)
                        ok = False
                    if ok:
                        deleted += 1
                    else:
                        logging.exception("Failed to quarantine %s: move returned False", obj)
                        continue
                except Exception as e:
                    logging.exception("Failed to quarantine %s: %s", obj, e)
                    continue
            else:
                # not confirmed; count as planned but don't execute
                deleted += 1

        return {"flushed": True if deleted > 0 else False, "deleted_count": deleted,
                "reason": f"no uploads for {THRESHOLD_HOURS} hours", "simulated": simulate, "sample": sample}

    finally:
        _release_lock(lock_fd)
