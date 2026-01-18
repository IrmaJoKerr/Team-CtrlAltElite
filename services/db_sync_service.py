import logging
import json
from typing import List, Dict, Any, Optional

LOG = logging.getLogger(__name__)


def acquire_advisory_lock(conn, key: int = 123456789) -> bool:
    cur = conn.cursor()
    cur.execute('SELECT pg_try_advisory_lock(%s)', (key,))
    ok = cur.fetchone()[0]
    cur.close()
    return ok


def release_advisory_lock(conn, key: int = 123456789):
    cur = conn.cursor()
    cur.execute('SELECT pg_advisory_unlock(%s)', (key,))
    cur.close()


def _parse_vector_text(vec_text: Optional[str]) -> Optional[List[float]]:
    if not vec_text:
        return None
    try:
        return json.loads(vec_text)
    except Exception:
        try:
            import ast
            parsed = ast.literal_eval(vec_text)
            # normalize tuples to lists
            if isinstance(parsed, tuple):
                return list(parsed)
            return parsed
        except Exception:
            return None


def fetch_batch_from_db(conn, batch_size: int) -> List[Dict[str, Any]]:
    cur = conn.cursor()
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
        vector = _parse_vector_text(vec_text)
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


def mark_synced(conn, ids: List[int]):
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS qdrant_sync_state (document_id INT PRIMARY KEY, last_synced_at TIMESTAMP DEFAULT now())")
    for doc_id in ids:
        cur.execute("INSERT INTO qdrant_sync_state (document_id, last_synced_at) VALUES (%s, now()) ON CONFLICT (document_id) DO UPDATE SET last_synced_at = now()", (doc_id,))
    conn.commit()
    cur.close()
