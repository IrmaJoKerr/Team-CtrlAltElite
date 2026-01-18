import os
import json
from typing import Any, Optional, List
from datetime import datetime, timezone
import uuid
from io import BytesIO
import logging

from pypdf import PdfReader


def gcs_write_json(bucket_name: str, path: str, obj: Any) -> None:
    """Write JSON to storage via adapter, falling back to GCS client if available.

    Interface matches the helper used in `docintel-data-processor.main`.
    """
    try:
        from adapters.storage_adapter import get_storage_adapter
        adapter = get_storage_adapter()
        object_name = f"{bucket_name}/{path}".lstrip('/')
        adapter.write_json(object_name, obj)
        return
    except Exception:
        # fallback to GCS if google storage client present
        try:
            from google import storage as gcs_storage  # type: ignore
        except Exception:
            gcs_storage = None

        if gcs_storage:
            try:
                client = gcs_storage.Client()
                bucket = client.bucket(bucket_name)
                blob = bucket.blob(path)
                blob.upload_from_string(json.dumps(obj, ensure_ascii=False, indent=2), content_type='application/json')
                return
            except Exception:
                logging.exception('GCS upload failed')

    logging.error(f"No storage backend available for writing JSON to {bucket_name}/{path}")
    raise RuntimeError("No storage backend available")


def extract_text_from_pdf_gs_uri(gs_uri: str) -> str:
    """Extract text from a PDF stored at a gs:// URI or local adapter path.

    Returns extracted text or raises on failure.
    """
    try:
        # parse gs://bucket/path
        if gs_uri.startswith('gs://'):
            _, rest = gs_uri.split('://', 1)
            parts = rest.split('/', 1)
            bucket = parts[0]
            name = parts[1] if len(parts) > 1 else ''
        else:
            bucket = ''
            name = gs_uri

        pdf_content = BytesIO()
        # try local adapter first
        try:
            from adapters.storage_adapter import get_storage_adapter
            adapter = get_storage_adapter()
            obj_name = f"{bucket}/{name}".lstrip('/')
            data = adapter.read_bytes(obj_name)
            pdf_content.write(data)
        except Exception:
            # fallback to GCS
            try:
                from google import storage as gcs_storage  # type: ignore
                client = gcs_storage.Client()
                bucket_obj = client.bucket(bucket)
                blob = bucket_obj.blob(name)
                data = blob.download_as_bytes()
                pdf_content.write(data)
            except Exception:
                logging.exception('No storage backend available or download failed')
                raise RuntimeError('No storage backend available')

        pdf_content.seek(0)
        reader = PdfReader(pdf_content)
        text = ''
        for page in reader.pages:
            text += (page.extract_text() or '') + '\n'
        return text
    except Exception as e:
        logging.error(f"Failed to extract text from PDF: {e}", exc_info=True)
        raise


def list_versions(bucket_name: str, prefix: str) -> List[str]:
    """List object names under a prefix via adapter or GCS client.

    Returns a list of object names (may be empty).
    """
    try:
        from adapters.storage_adapter import get_storage_adapter
        adapter = get_storage_adapter()
        full_prefix = f"{bucket_name}/{prefix}".lstrip('/')
        return adapter.list_objects(prefix=full_prefix)
    except Exception:
        try:
            from google import storage as gcs_storage  # type: ignore
        except Exception:
            gcs_storage = None

        if gcs_storage:
            try:
                client = gcs_storage.Client()
                bucket = client.bucket(bucket_name)
                return [b.name for b in bucket.list_blobs(prefix=prefix)]
            except Exception:
                logging.exception('GCS list blobs failed')
                return []

        logging.error(f"No storage backend available for listing blobs for {bucket_name}/{prefix}")
        return []


def append_audit(bucket_name: str, base_path: str, entry: Any) -> None:
    """Write an audit entry under `base_path/audit/` using `gcs_write_json`.

    The filename is timestamped and suffixed with a random UUID.
    """
    ts = datetime.now(timezone.utc).isoformat()
    tid = uuid.uuid4().hex
    path = f"{base_path}/audit/{ts}_{tid}.json"
    gcs_write_json(bucket_name, path, entry)
