import logging
from typing import List, Dict, Any, Optional

from adapters import qdrant_adapter
from services import db_sync_service

logging.basicConfig(level=logging.INFO)


class SyncOutboxService:
    """Orchestrates fetching embedding rows from DB and upserting to Qdrant."""

    def __init__(self, qdrant_collection: str, qdrant_url: str):
        self.qdrant_collection = qdrant_collection
        self.qdrant_url = qdrant_url

    def run_once(self, conn, batch_size: int = 50, dry_run: bool = False) -> bool:
        """Run a single iteration: fetch a batch, upsert to Qdrant, mark synced.

        Returns True on success (or nothing to do), False on fatal failure.
        """
        locked = False
        try:
            locked = db_sync_service.acquire_advisory_lock(conn)
            if not locked:
                logging.error('Could not acquire advisory lock; another worker may be running')
                return False

            rows = db_sync_service.fetch_batch_from_db(conn, batch_size)
            if not rows:
                logging.info('No documents with embeddings to sync')
                return True

            points = []
            ids = []
            for r in rows:
                if not r.get('vector'):
                    logging.warning('Document %s has no vector; skipping', r.get('id'))
                    continue
                points.append({'id': r['id'], 'vector': r['vector'], 'payload': {'title': r.get('title'), 'department': r.get('department'), 'gcs_path': r.get('gcs_path')}})
                ids.append(r['id'])

            if not points:
                logging.info('No upsertable points in this batch')
                return True

            ok = qdrant_adapter.upsert_points(points, self.qdrant_collection, self.qdrant_url, dry_run=dry_run)
            if ok:
                db_sync_service.mark_synced(conn, ids)
                return True
            else:
                logging.error('Failed to upsert points to Qdrant; aborting')
                return False

        finally:
            if locked:
                try:
                    db_sync_service.release_advisory_lock(conn)
                except Exception:
                    logging.exception('Failed releasing advisory lock')


def build_points_from_local(fixtures: List[Dict[str, Any]], embedding_fn) -> List[Dict[str, Any]]:
    texts = [r.get('content') or '' for r in fixtures]
    vectors = embedding_fn(texts)
    points = []
    for r, v in zip(fixtures, vectors):
        points.append({'id': r.get('id'), 'vector': v, 'payload': {'title': r.get('title'), 'department': r.get('department'), 'gcs_path': r.get('gcs_path')}})
    return points
