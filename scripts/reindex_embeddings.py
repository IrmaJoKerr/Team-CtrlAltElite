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

if not DB_PASSWORD:
    logging.error('DB_PASSWORD not set in environment. Provide password or run via Cloud SQL Auth Proxy.')
    sys.exit(1)

import psycopg2


def connect():
    return psycopg2.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME)


def chunked(iterable, n):
    for i in range(0, len(iterable), n):
        yield iterable[i:i+n]


def main():
    parser = argparse.ArgumentParser(description='Reindex document embeddings')
    parser.add_argument('--batch-size', type=int, default=int(os.environ.get('REINDEX_BATCH_SIZE', '100')))
    parser.add_argument('--force', action='store_true', help='Recompute embeddings for all documents')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    batch_size = args.batch_size
    force = args.force
    dry_run = args.dry_run

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

    conn = connect()
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
