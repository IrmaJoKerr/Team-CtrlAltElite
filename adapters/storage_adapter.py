import os
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
import shutil
import uuid
import tempfile
import errno


class LocalStorageAdapter:
    def __init__(self, root: Optional[str] = None):
        self.root = root or os.environ.get("STORAGE_ROOT", "local_test_store")
        # ensure root exists
        Path(self.root).mkdir(parents=True, exist_ok=True)

    def _abs_path(self, object_name: str) -> str:
        # store under documents/ to mirror earlier layout
        p = Path(self.root) / "documents" / object_name
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    def upload_bytes(self, object_name: str, data: bytes, content_type: Optional[str] = None) -> str:
        """Write bytes to local storage and return a file URI.

        Returns: file:// absolute path
        """
        path = self._abs_path(object_name)
        # write bytes
        with open(path, "wb") as f:
            f.write(data)
        return f"file://{os.path.abspath(path)}"

    def write_json(self, object_name: str, obj) -> str:
        path = self._abs_path(object_name)
        with open(path, "w", encoding="utf-8") as f:
            import json
            json.dump(obj, f, ensure_ascii=False, indent=2)
        return f"file://{os.path.abspath(path)}"

    def read_bytes(self, object_name: str) -> bytes:
        path = self._abs_path(object_name)
        with open(path, "rb") as f:
            return f.read()

    def list_objects(self, prefix: Optional[str] = None) -> list:
        """List object names under documents/ optionally filtered by prefix.

        Prefix should be relative like 'bucket/path' or 'bucket/'.
        Returns list of object names (using forward slashes) relative to documents/ root.
        """
        base = Path(self.root) / "documents"
        if not base.exists():
            return []
        results = []
        for p in base.rglob('*'):
            if p.is_file():
                rel = p.relative_to(base).as_posix()
                if prefix:
                    if rel.startswith(prefix):
                        results.append(rel)
                else:
                    results.append(rel)
        return results

    def get_latest_timestamp(self, prefix: Optional[str] = None):
        """Return newest modification time (UTC) for objects matching prefix.

        Returns a timezone-aware datetime in UTC, or None if no objects.
        """
        base = Path(self.root) / "documents"
        if not base.exists():
            return None
        newest_ts = None
        for p in base.rglob("*"):
            if p.is_file():
                rel = p.relative_to(base).as_posix()
                if prefix and not rel.startswith(prefix):
                    continue
                try:
                    m = p.stat().st_mtime
                except Exception:
                    continue
                if newest_ts is None or m > newest_ts:
                    newest_ts = m
        if newest_ts is None:
            return None
        return datetime.fromtimestamp(newest_ts, tz=timezone.utc)

    def move_object(self, src: str, dest: str) -> bool:
        """Move an object from src to dest within the adapter's storage.

        Tries an atomic rename first; falls back to streaming copy+delete on failure.
        Returns True on success, False on failure.
        """
        src_path = Path(self._abs_path(src))
        dest_path = Path(self._abs_path(dest))
        # Ensure destination parent exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Fast path: try atomic replace (works when on same FS)
        try:
            os.replace(str(src_path), str(dest_path))
            return True
        except OSError as e:
            # If error is EXDEV (cross-device), we'll do copy-then-rename via temp in dest dir
            pass
        except Exception:
            # Other failures fall through to safe copy path
            pass

        # Slow path: write to a temp file in the destination directory, fsync, then atomically replace
        temp_name = dest_path.name + f".tmp-{uuid.uuid4().hex}"
        temp_path = dest_path.parent / temp_name
        try:
            # Stream-copy src -> temp
            with open(src_path, 'rb') as r, open(temp_path, 'wb') as w:
                shutil.copyfileobj(r, w, length=16 * 1024)
                w.flush()
                try:
                    os.fsync(w.fileno())
                except OSError:
                    # best-effort fsync
                    pass

            # Atomic rename temp -> dest
            os.replace(str(temp_path), str(dest_path))

            # Attempt to fsync parent dir for durability (best-effort)
            try:
                dir_fd = os.open(str(dest_path.parent), os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass

            # Remove source
            try:
                os.remove(src_path)
            except Exception:
                # If removing source fails, we leave the dest in place and report success
                pass
            return True
        except Exception:
            # Cleanup partial temp if any
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            return False

    def delete_object(self, object_name: str) -> bool:
        path = self._abs_path(object_name)
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False


def get_storage_adapter() -> LocalStorageAdapter:
    return LocalStorageAdapter()


def get_latest_timestamp(prefix: Optional[str] = None):
    """Module-level convenience to ask the default adapter for latest timestamp."""
    return get_storage_adapter().get_latest_timestamp(prefix)


def move_object(src: str, dest: str) -> bool:
    """Module-level convenience to move objects using the default adapter."""
    return get_storage_adapter().move_object(src, dest)
