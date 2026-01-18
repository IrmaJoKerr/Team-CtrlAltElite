"""
Backfill script to migrate existing JSON/text embeddings into the new `embedding_vector` column.

Usage: set environment vars `DB_HOST`, `DB_USER`, `DB_NAME`, and ensure DB password is available
via the environment variable `DB_PASSWORD`, then run:

python scripts/backfill_embeddings.py
"""
import os
import json
import logging
import psycopg2

logging.basicConfig(level=logging.INFO)

DB_HOST = os.environ.get('DB_HOST')
DB_USER = os.environ.get('DB_USER', 'sop_user')
DB_NAME = os.environ.get('DB_NAME', 'sop_database')
def main():
    from utils.cli import build_parser, get_effective_config

    parser = build_parser()
    ns = parser.parse_args()
    cfg = get_effective_config()

    # Establish DB connection using secrets adapter when needed
    try:
        from adapters.secrets_adapter import get_db_password
        pw = get_db_password(cloud_mode=cfg.cloud_mode, config=cfg)
    except Exception:
        logging.error("DB password not available; set DB_PASSWORD or configure secret provider.")
        raise SystemExit(1)

    conn = psycopg2.connect(host=DB_HOST, user=DB_USER, password=pw, dbname=DB_NAME)
    cursor = conn.cursor()

    # Select rows that have the old embedding column populated (if still present)
    try:
        cursor.execute("SELECT id, embedding FROM documents WHERE embedding IS NOT NULL")
    except Exception:
        logging.info("No old 'embedding' column or no rows to backfill. Exiting.")
        cursor.close()
        conn.close()
        raise SystemExit(0)

    rows = cursor.fetchall()
    logging.info(f"Found {len(rows)} rows to backfill.")

    for row in rows:
        doc_id, embedding_text = row
        try:
            vect = json.loads(embedding_text)
            if not isinstance(vect, list):
                logging.warning(f"Row {doc_id}: embedding not a list; skipping.")
                continue
            emb_str = '[' + ','.join(map(str, vect)) + ']'
            cursor.execute("UPDATE documents SET embedding_vector = %s::vector, embedding = NULL WHERE id = %s", (emb_str, doc_id))
        except Exception as e:
            logging.error(f"Failed to backfill row {doc_id}: {e}")

    conn.commit()
    cursor.close()
    conn.close()
    logging.info("Backfill completed. Run ANALYZE on the table and then recreate ivfflat index if desired.")


if __name__ == '__main__':
    main()
