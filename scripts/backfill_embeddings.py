"""
Backfill script to migrate existing JSON/text embeddings into the new `embedding_vector` column.

Usage: set environment vars `DB_HOST`, `DB_USER`, `DB_NAME`, and ensure DB password is available
via Secret Manager or env `DB_PASSWORD`, then run:

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
DB_PASSWORD = os.environ.get('DB_PASSWORD')

if not DB_PASSWORD:
    logging.error("DB_PASSWORD not set in environment. Provide password or run via Cloud SQL Auth Proxy.")
    raise SystemExit(1)

conn = psycopg2.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME)
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
