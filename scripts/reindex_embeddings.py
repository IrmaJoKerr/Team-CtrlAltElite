#!/usr/bin/env python3
"""Recompute and upsert embeddings for documents using the embedding adapter.

Usage:
  export DB_HOST=... DB_USER=... DB_NAME=... DB_PASSWORD=...
  python3 scripts/reindex_embeddings.py --batch-size 100 --force

Options:
  --batch-size N   Number of documents to process per DB round (default 100)
  --force          Recompute embeddings for all documents (default: only NULL vectors)
  --dry-run        Show work but do not write to DB
"""
import os
import sys
import time
import logging
import argparse
from typing import List

logging.basicConfig(level=logging.INFO)

DB_HOST = os.environ.get('DB_HOST')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'docintel_db')
DB_PASSWORD = os.environ.get('DB_PASSWORD')

from adapters.secrets_adapter import get_db_password

import psycopg2


def connect(config=None):
    # config: utils.config.Config or object with cloud_mode/secret_provider
    cloud_mode = getattr(config, 'cloud_mode', False) if config is not None else False
    pw = get_db_password(cloud_mode=cloud_mode, config=config)
    return psycopg2.connect(host=DB_HOST, user=DB_USER, password=pw, dbname=DB_NAME)


def chunked(iterable, n):
    for i in range(0, len(iterable), n):
        yield iterable[i:i+n]


def main():
    from utils.cli import build_parser

    parser = build_parser()
    parser.add_argument('--force', action='store_true', help='Recompute embeddings for all documents')
    parser.add_argument('--dry-run', action='store_true')
    ns = parser.parse_args()

    cfg = None
    try:
        from utils.cli import get_effective_config
        cfg = get_effective_config()
    except Exception:
        # fallback: build from parsed namespace
        class _F:
            mode = getattr(ns, 'mode', 'local')
            simulate = ns.__dict__.get('dry_run', True)
            cloud_mode = True if mode == 'cloud' else False
        cfg = _F()

    batch_size = ns.batch_size
    force = ns.force
    dry_run = ns.dry_run or getattr(cfg, 'simulate', True)

    logging.info(f"Reindex embeddings: batch_size={batch_size} force={force} dry_run={dry_run}")

    # Import embedding adapter lazily
    try:
        from adapters.embedding_adapter import get_embeddings
    except Exception:
        # adapter exposes get_embeddings as the function name in our implementation
        try:
            from adapters.embedding_adapter import get_embeddings as get_embeddings
        except Exception as e:
            logging.error('Failed to import embedding adapter: %s', e)
            sys.exit(1)

    conn = connect(cloud_mode=getattr(cfg, 'cloud_mode', False))
    cur = conn.cursor()

    total_processed = 0
    while True:
        if force:
            cur.execute("SELECT id, chunk_content FROM documents ORDER BY id LIMIT %s", (batch_size,))
        else:
            cur.execute("SELECT id, chunk_content FROM documents WHERE embedding_vector IS NULL ORDER BY id LIMIT %s", (batch_size,))
        rows = cur.fetchall()
        if not rows:
            logging.info('No more documents to process')
            break

        ids = [r[0] for r in rows]
        texts = [(r[1] or '') for r in rows]

        try:
            embeddings = get_embeddings(texts)
        except Exception as e:
            logging.exception('Embedding generation failed: %s', e)
            # abort to avoid rapidly looping on failures
            break

        if len(embeddings) != len(ids):
            logging.error('Embedding count mismatch: %s vs %s', len(embeddings), len(ids))
            break

        # Upsert embeddings back to DB
        for doc_id, emb in zip(ids, embeddings):
            emb_str = '[' + ','.join(map(str, emb)) + ']'
            if dry_run:
                logging.info('[DRY RUN] Would update doc %s with vector dim=%d', doc_id, len(emb))
                continue
            try:
                cur.execute("UPDATE documents SET embedding_vector = %s::vector WHERE id = %s", (emb_str, doc_id))
            except Exception:
                logging.exception('Failed to update embedding for doc %s', doc_id)
        if not dry_run:
            conn.commit()

        total_processed += len(ids)
        logging.info('Processed %d documents (total %d)', len(ids), total_processed)

        # short sleep to avoid hammering provider/db
        time.sleep(0.1)

    cur.close()
    conn.close()
    logging.info('Reindex complete. Total processed: %d', total_processed)


if __name__ == '__main__':
    main()
