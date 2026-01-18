#!/usr/bin/env python3
"""Sync documents with embeddings from Postgres to Qdrant.

Features:
- PG advisory lock to prevent concurrent runs
- Idempotent upserts using document `id` as point id
- Batch processing with configurable size
- Retry/backoff on network errors
- `--simulate` mode to run locally against `local_test_store` without DB

Usage:
  # real run (requires DB + Qdrant):
  export DB_HOST=... DB_USER=... DB_NAME=... DB_PASSWORD=... QDRANT_URL=http://localhost:6333
  python3 scripts/sync_outbox_to_qdrant.py --once

  # simulate using local fixtures (no DB required):
  python3 scripts/sync_outbox_to_qdrant.py --simulate --dry-run

"""
import os
import sys
import time
import json
import logging
import argparse
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO)

DB_HOST = os.environ.get('DB_HOST')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'docintel_db')
DB_PASSWORD = os.environ.get('DB_PASSWORD')

from adapters.secrets_adapter import get_db_password
from services.db_sync_service import acquire_advisory_lock, release_advisory_lock, fetch_batch_from_db, mark_synced

QDRANT_URL = os.environ.get('QDRANT_URL', 'http://localhost:6333')
QDRANT_COLLECTION = os.environ.get('QDRANT_COLLECTION', 'documents')

try:
    import psycopg2
except Exception:
    psycopg2 = None

import requests


def acquire_advisory_lock(conn) -> bool:
    cur = conn.cursor()
    # use a stable hash as key; here we choose a fixed pair
    cur.execute('SELECT pg_try_advisory_lock(%s)', (123456789,))
    ok = cur.fetchone()[0]
    cur.close()
    return ok


def release_advisory_lock(conn):
    cur = conn.cursor()
    cur.execute('SELECT pg_advisory_unlock(%s)', (123456789,))
    cur.close()


def fetch_batch_from_db(conn, batch_size: int) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    # select documents that have embeddings
    cur.execute(
        """
        SELECT id, chunk_content, embedding_vector::text, final_title, final_department, gcs_object_path, updated_at
        FROM documents
        WHERE embedding_vector IS NOT NULL
        ORDER BY updated_at ASC
        LIMIT %s
        """,
        (batch_size,)
    )
    rows = cur.fetchall()
    cur.close()
    results = []
    for r in rows:
        doc_id, content, vec_text, title, dept, gcs_path, updated_at = r
        # parse vector text like '[0.1,0.2,...]'
        try:
            vector = json.loads(vec_text)
        except Exception:
            # fallback: try literal eval
            try:
                import ast
                vector = ast.literal_eval(vec_text)
            except Exception:
                vector = None
        results.append({
            'id': doc_id,
            'content': content,
            'vector': vector,
            'title': title,
            'department': dept,
            'gcs_path': gcs_path,
            'updated_at': updated_at,
        })
    return results


def fetch_batch_from_local_test_store(batch_size: int) -> List[Dict[str, Any]]:
    base = os.path.join('local_test_store', 'documents')
    if not os.path.exists(base):
        return []
    files = []
    for root, _, fnames in os.walk(base):
        for fn in fnames:
            if fn.endswith('.json'):
                files.append(os.path.join(root, fn))
    files = sorted(files)[:batch_size]
    results = []
    for f in files:
        with open(f, 'r', encoding='utf-8') as fh:
            try:
                data = json.load(fh)
            except Exception:
                continue
        results.append({
            'id': data.get('document_id') or os.path.basename(f),
            'content': data.get('final_title') or data.get('original_gcs_filename'),
            'vector': None,
            'title': data.get('final_title'),
            'department': data.get('department_folder') or data.get('final_department'),
            'gcs_path': data.get('original_gcs_filename') or f,
        })
    return results


def upsert_points_to_qdrant(points: List[Dict[str, Any]], collection: str, qdrant_url: str, dry_run: bool = False) -> bool:
    # Delegate to qdrant adapter for network logic, retries, and dry-run handling
    try:
        from adapters.qdrant_adapter import upsert_points as _adapter_upsert
        return _adapter_upsert(points, collection, qdrant_url, dry_run=dry_run)
    except Exception:
        logging.exception('Qdrant adapter failed')
        return False


def mark_synced(conn, ids: List[int]):
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS qdrant_sync_state (document_id INT PRIMARY KEY, last_synced_at TIMESTAMP DEFAULT now())")
    for doc_id in ids:
        cur.execute("INSERT INTO qdrant_sync_state (document_id, last_synced_at) VALUES (%s, now()) ON CONFLICT (document_id) DO UPDATE SET last_synced_at = now()", (doc_id,))
    conn.commit()
    cur.close()


def main():
    from utils.cli import build_parser, get_effective_config
    from services.sync_outbox_service import SyncOutboxService, build_points_from_local

    parser = build_parser()
    parser.add_argument('--batch-size', type=int, default=50)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--simulate', action='store_true', help='Use local_test_store fixtures instead of DB')
    ns = parser.parse_args()

    cfg = get_effective_config()
    batch_size = ns.batch_size
    dry_run = ns.dry_run or getattr(cfg, 'simulate', False)
    simulate = ns.simulate or getattr(cfg, 'simulate', False)
    cloud_mode = getattr(cfg, 'cloud_mode', False)

    logging.info('Starting sync_outbox_to_qdrant (mode=%s simulate=%s dry_run=%s batch=%d)', getattr(cfg, 'mode', None), simulate, dry_run, batch_size)

    if simulate:
        rows = fetch_batch_from_local_test_store(batch_size)
        if not rows:
            logging.info('No fixtures found in local_test_store/documents')
            return
        # try to compute vectors using embedding adapter if available
        try:
            from adapters.embedding_adapter import get_embeddings
            points = build_points_from_local(rows, get_embeddings)
        except Exception:
            logging.exception('Failed to generate embeddings for simulated rows; leaving vectors None')
            points = [{'id': r['id'], 'vector': r.get('vector'), 'payload': {'title': r.get('title'), 'department': r.get('department'), 'gcs_path': r.get('gcs_path')}} for r in rows]

        try:
            from adapters.qdrant_adapter import upsert_points as _adapter_upsert
            ok = _adapter_upsert(points, QDRANT_COLLECTION, QDRANT_URL, dry_run=dry_run)
        except Exception:
            logging.exception('Qdrant adapter failed')
            ok = False

        if ok:
            logging.info('Simulated sync complete (ok=%s)', ok)
        else:
            logging.error('Simulated sync failed')
        return

    # real run path
    if not simulate:
        if psycopg2 is None:
            logging.error('psycopg2 not available; cannot run against DB')
            return
        try:
            pw = get_db_password(cloud_mode=cloud_mode, config=cfg)
        except Exception as e:
            logging.error('Failed to obtain DB password: %s', e)
            return

        conn = psycopg2.connect(host=DB_HOST, user=DB_USER, password=pw, dbname=DB_NAME)

    service = SyncOutboxService(QDRANT_COLLECTION, QDRANT_URL)
    try:
        while True:
            ok = service.run_once(conn, batch_size=batch_size, dry_run=dry_run)
            if not ok:
                break
            if ns.once:
                break
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
