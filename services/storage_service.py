import os
import json
from typing import Any, Optional, List
from datetime import datetime, timezone
import uuid
from io import BytesIO
import logging

from pypdf import PdfReader


def gcs_write_json(bucket_name: str, path: str, obj: Any) -> None:
    """Write JSON to storage via the configured storage adapter.

    Interface matches the helper used in `docintel-data-processor.main`.
    This service relies on the storage adapter; it no longer attempts to
    use cloud SDKs directly. If no adapter is available, a RuntimeError is raised.
    """
    try:
        from adapters.storage_adapter import get_storage_adapter

        adapter = get_storage_adapter()
        object_name = f"{bucket_name}/{path}".lstrip("/")
        adapter.write_json(object_name, obj)
        return
    except Exception:
        logging.exception("Storage adapter unavailable for write_json")
        raise RuntimeError("No storage backend available; configure a storage adapter")


def extract_text_from_pdf_gs_uri(gs_uri: str) -> str:
    """Extract text from a PDF located via a storage adapter path or URI.

    This function uses the storage adapter to read bytes and then parses
    the PDF. It no longer falls back to cloud SDKs directly.
    """
    try:
        # parse object-storage URI if present
        if gs_uri.startswith("gs://"):
            _, rest = gs_uri.split("://", 1)
            parts = rest.split("/", 1)
            bucket = parts[0]
            name = parts[1] if len(parts) > 1 else ""
        else:
            bucket = ""
            name = gs_uri

        pdf_content = BytesIO()
        from adapters.storage_adapter import get_storage_adapter

        adapter = get_storage_adapter()
        obj_name = f"{bucket}/{name}".lstrip("/")
        data = adapter.read_bytes(obj_name)
        pdf_content.write(data)

        pdf_content.seek(0)
        reader = PdfReader(pdf_content)
        text = ""
        for page in reader.pages:
            text += (page.extract_text() or "") + "\n"
        return text
    except Exception as e:
        logging.exception("Failed to extract text from PDF via storage adapter: %s", e)
        raise RuntimeError("Failed to extract text from PDF: no storage backend available")


def extract_text_from_bytes(data: bytes, filename: Optional[str] = None) -> str:
    """Extract text from uploaded bytes.

    - If `filename` indicates a PDF (.pdf) the PDF is parsed via `PdfReader`.
    - Otherwise data is decoded as UTF-8 text.

    This consolidates upload-time text extraction into the storage service
    so callers (including `main.py`) can delegate extraction logic.
    """
    try:
        if filename and filename.lower().endswith('.pdf'):
            reader = PdfReader(BytesIO(data))
            text = ''
            for page in reader.pages:
                text += (page.extract_text() or '') + '\n'
            return text
        else:
            return data.decode('utf-8')
    except Exception:
        logging.exception('Failed to extract text from bytes')
        raise


def list_versions(bucket_name: str, prefix: str) -> List[str]:
    """List object names under a prefix via the storage adapter.

    Returns a list of object names (may be empty). Raises RuntimeError if
    no adapter is available.
    """
    try:
        from adapters.storage_adapter import get_storage_adapter

        adapter = get_storage_adapter()
        full_prefix = f"{bucket_name}/{prefix}".lstrip("/")
        return adapter.list_objects(prefix=full_prefix)
    except Exception:
        logging.exception("Storage adapter unavailable for list_versions")
        raise RuntimeError("No storage backend available for listing objects")


def append_audit(bucket_name: str, base_path: str, entry: Any) -> None:
    """Write an audit entry under `base_path/audit/` using the storage adapter.

    The filename is timestamped and suffixed with a random UUID.
    """
    ts = datetime.now(timezone.utc).isoformat()
    tid = uuid.uuid4().hex
    path = f"{base_path}/audit/{ts}_{tid}.json"
    gcs_write_json(bucket_name, path, entry)


def write_json_path(path: str, obj: Any) -> None:
    """Write JSON to an explicit filesystem path (used by main.py helpers).

    This mirrors the previous `local_write_json` behavior.
    """
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_json_path(path: str) -> Optional[Any]:
    """Read JSON from an explicit filesystem path; returns None if missing."""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def delete_object(bucket_name: str, object_name: str) -> bool:
    """Delete an object using the storage adapter.

    Returns True on success. Raises RuntimeError if no adapter available.
    """
    try:
        from adapters.storage_adapter import get_storage_adapter

        adapter = get_storage_adapter()
        obj_name = f"{bucket_name}/{object_name}".lstrip("/")
        return adapter.delete_object(obj_name)
    except Exception:
        logging.exception("Storage adapter unavailable for delete_object")
        raise RuntimeError("No storage backend available to delete object")
