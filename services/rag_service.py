import asyncio
import logging
from typing import List, Dict, Any, Optional

import services.embedding_service as embedding_service
import services.db_service as db_service

LOG = logging.getLogger(__name__)


async def run_rag_query(query: str, department: Optional[str] = None, top_k: int = 3) -> Dict[str, Any]:
    """Orchestrate embedding generation and DB search, returning prompt and snippets.

    This function keeps sync DB operations inside `asyncio.to_thread` to avoid
    blocking the event loop.
    """
    # Generate embedding
    emb = await embedding_service.get_text_embeddings([query])
    qvec = emb[0]
    qvec_str = '[' + ','.join(map(str, qvec)) + ']'

    def _sync_search():
        conn = db_service.get_db_connection()
        cursor = None
        try:
            cursor = conn.cursor()
            where = ''
            params = []
            if department:
                where = 'WHERE department_folder = %s'
                params.append(department)
            sql = f"SELECT chunk_content, gcs_object_path FROM documents {where} ORDER BY embedding_vector <-> %s::vector LIMIT %s"
            params.extend([qvec_str, top_k])
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
            snippets = [{'path': r[1], 'snippet': r[0]} for r in rows]
            return snippets
        except Exception:
            LOG.exception('pgvector search failed, falling back to text search')
            try:
                if department:
                    cursor.execute("SELECT chunk_content, gcs_object_path FROM documents WHERE department_folder = %s AND chunk_content ILIKE %s LIMIT %s", (department, f"%{query}%", top_k))
                else:
                    cursor.execute("SELECT chunk_content, gcs_object_path FROM documents WHERE chunk_content ILIKE %s LIMIT %s", (f"%{query}%", top_k))
                rows = cursor.fetchall()
                snippets = [{'path': r[1], 'snippet': r[0]} for r in rows]
                return snippets
            except Exception:
                LOG.exception('Text search fallback also failed')
                return []
        finally:
            try:
                if cursor:
                    cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    snippets = await asyncio.to_thread(_sync_search)

    context_text = '\n\n'.join([s['snippet'] for s in snippets])[:4000]
    prompt = f"Answer the user query using the following document snippets. Query: {query}\n\nSnippets:\n{context_text}\n\nProvide a concise answer and list sources."

    return {'prompt': prompt, 'snippets': snippets}
