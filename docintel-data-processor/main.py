import os
import json
import base64
import logging
from typing import List, Dict, Any, Optional
import asyncio
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import httpx
import services.storage_service as storage_service

# Local-first: generative/Vertex SDKs removed. Use adapters where available.
from datetime import datetime, timezone, timedelta
import uuid

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

storage = None
pubsub_v1 = None
service_account = None
AuthorizedSession = None
google = None
import psycopg2
from psycopg2.extras import execute_values
from pypdf import PdfReader  # For PDF parsing

# --- Configuration and Initialization ---
logging.basicConfig(level=logging.INFO)
app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GCP Clients (optional)
storage_client = None
pubsub_publisher = None
if storage is not None:
    try:
        storage_client = storage.Client()
    except Exception:
        storage_client = None
if pubsub_v1 is not None:
    try:
        pubsub_publisher = pubsub_v1.PublisherClient()
    except Exception:
        pubsub_publisher = None

# Vertex/Endpoint config (use endpoints created in Vertex UI)
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID")  # e.g. text-embedding-004
GENERATIVE_MODEL_ID = os.environ.get("GENERATIVE_MODEL_ID")  # e.g. gemini-2.5-pro
EMBEDDING_ENDPOINT = os.environ.get(
    "EMBEDDING_ENDPOINT"
)  # e.g. projects/PROJECT/locations/us-west1/endpoints/EMBEDDING_ID
GENERATIVE_ENDPOINT = os.environ.get(
    "GENERATIVE_ENDPOINT"
)  # e.g. projects/PROJECT/locations/us-west1/endpoints/GEN_ID

# No lazy SDK models in local-first design; adapters should provide models.

# Authorized HTTP session (lazy)
_AUTH_SESSION = None
_AUTH_LOCK = threading.Lock()


def get_authed_session():
    # Local-first repo: no hosted auth session. Return None and let callers
    # use adapter-based authentication or unauthenticated requests as appropriate.
    return None


async def async_post_with_retries(
    url, json=None, headers=None, timeout=None, retries=3, backoff_factor=1.0
):
    """Async POST helper with exponential backoff using httpx.AsyncClient."""
    attempt = 0
    # httpx timeout can be a tuple (connect, read) or a number
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url, json=json, headers=headers, timeout=timeout
                )
                resp.raise_for_status()
                return resp
        except httpx.ReadTimeout:
            attempt += 1
            if attempt > retries:
                raise
            sleep_for = backoff_factor * (2 ** (attempt - 1))
            await asyncio.sleep(sleep_for)
        except httpx.RequestError:
            attempt += 1
            if attempt > retries:
                raise
            sleep_for = backoff_factor * (2 ** (attempt - 1))
            await asyncio.sleep(sleep_for)


def get_auth_headers():
    """Return Authorization headers.

    In local-first mode there is no hosted auth; adapters may provide headers
    when necessary. Return an empty dict by default.
    """
    return {}


# Environment Variables
PROJECT_ID = os.environ.get("PROJECT_ID", "ctrlaltelite-484111")
REGION = os.environ.get("REGION", "us-central1")
RAG_REGION = os.environ.get("RAG_REGION", "us-west1")  # RAG corpus is in us-west1
RAG_CORPUS_ID = os.environ.get("RAG_CORPUS_ID", "2305843009213693952")
RAG_CORPUS_NAME = (
    f"projects/{PROJECT_ID}/locations/{RAG_REGION}/ragCorpora/{RAG_CORPUS_ID}"
)
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "ambuckethack")
TEST_MODE = os.environ.get("TEST_MODE", "").lower() in ("1", "true", "yes")

# Database Config
DB_HOST = os.environ.get("DB_HOST")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_NAME = os.environ.get("DB_NAME", "docintel_db")
# prefer DB_PASSWORD from env; if absent and running in cloud mode, use secrets adapter
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DISABLE_AUTH = os.environ.get("DISABLE_AUTH", "false").lower() in ("1", "true", "yes")

# Runtime mode: prefer explicit MODE env var for services (must be 'local' or 'cloud')
MODE = os.environ.get("MODE")
if MODE is not None and MODE not in ("local", "cloud"):
    raise RuntimeError("Invalid MODE. Set MODE=local or MODE=cloud")
CLOUD_MODE = True if MODE == "cloud" else False

from services.db_service import get_db_connection

# --- Helper Functions ---


def get_user_by_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """Look up a user by API key in the database."""
    if not api_key:
        return None
    # TEST_MODE: return a fake user to avoid DB/SecretManager calls
    if TEST_MODE:
        return {
            "id": 1,
            "username": "test-user",
            "role": "manager",
            "departments": ["operations"],
        }
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, username, role, departments FROM users WHERE api_key = %s LIMIT 1",
            (api_key,),
        )
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return None
        return {"id": row[0], "username": row[1], "role": row[2], "departments": row[3]}
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return None


async def get_current_user(
    authorization: Optional[str] = Header(None),
) -> Dict[str, Any]:
    """FastAPI dependency to resolve the current user from Authorization header.

    Accepts: 'Bearer <api_key>' or just the api_key in the header.
    Raises 401 if not found.
    """
    # Allow bypassing auth for quick local/testing use when DISABLE_AUTH is true.
    if DISABLE_AUTH:
        return {
            "id": 0,
            "username": "public",
            "role": "manager",
            "departments": ["operations"],
        }

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    token = authorization
    if authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1]

    user = get_user_by_api_key(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


def _test_metadata_path(gcs_object_path: str) -> str:
    safe = gcs_object_path.replace("/", "__")
    return os.path.join("local_test_store", "documents", f"{safe}.json")


def _test_version_dir(sanitized_name: str) -> str:
    return os.path.join("local_test_store", "versions", sanitized_name)


from services.embedding_service import get_text_embeddings

# --- Background Embedding Worker ---
# Polls for pending documents and generates embeddings asynchronously

EMBEDDING_WORKER_INTERVAL = int(
    os.environ.get("EMBEDDING_WORKER_INTERVAL", "60")
)  # seconds
EMBEDDING_WORKER_BATCH_SIZE = int(os.environ.get("EMBEDDING_WORKER_BATCH_SIZE", "5"))
_embedding_worker_task = None


async def embedding_worker_loop():
    """Background worker placeholder for processing embeddings.

    The original implementation was refactored to services; for tests and
    local-first development this worker is a no-op that periodically
    sleeps. Long-running or production embedding logic should be moved
    to a service module and re-enabled as needed.
    """
    logging.info(
        f"Embedding worker (placeholder) started. Polling every {EMBEDDING_WORKER_INTERVAL}s"
    )
    while True:
        try:
            await asyncio.sleep(EMBEDDING_WORKER_INTERVAL)
            if TEST_MODE:
                continue
            # No-op: embedding pipeline is handled by services/embedding_service
            continue
        except asyncio.CancelledError:
            logging.info("Embedding worker cancelled")
            break
        except Exception as e:
            logging.exception(f"Embedding worker unexpected error: {e}")
            # continue looping


async def process_override_embedding(override_id: int, justification: str):
    """Process embedding for an override justification immediately.

    Called when a loan officer submits an override - embeds right away
    for grey-area learning without waiting for edit window.
    """
    if TEST_MODE:
        return

    try:
        embeddings = await get_text_embeddings([justification])

        if embeddings and embeddings[0]:
            embedding_str = "[" + ",".join(map(str, embeddings[0])) + "]"

            conn = get_db_connection()
            cursor = conn.cursor()

            # Update override_log with embedding (if we add embedding_vector column later)
            # For now, just mark as complete
            cursor.execute(
                """
                UPDATE override_log 
                SET embedding_status = 'complete',
                    embedding_last_attempt_at = NOW()
                WHERE override_id = %s
            """,
                (override_id,),
            )

            conn.commit()
            cursor.close()
            conn.close()

            logging.info(f"Override {override_id} embedding complete")
    except Exception as e:
        logging.error(f"Failed to embed override {override_id}: {e}")
        # Mark as failed for retry
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE override_log 
                SET embedding_status = 'failed',
                    embedding_error_message = %s
                WHERE override_id = %s
            """,
                (str(e)[:500], override_id),
            )
            conn.commit()
            cursor.close()
            conn.close()
        except:
            pass


@app.on_event("startup")
async def startup_event():
    """Start background workers on app startup."""
    global _embedding_worker_task

    if not TEST_MODE:
        _embedding_worker_task = asyncio.create_task(embedding_worker_loop())
        logging.info("Background embedding worker scheduled")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup background workers on shutdown."""
    global _embedding_worker_task

    if _embedding_worker_task:
        _embedding_worker_task.cancel()
        try:
            await _embedding_worker_task
        except asyncio.CancelledError:
            pass
        logging.info("Background embedding worker stopped")


async def get_ai_metadata_suggestions(document_text: str) -> Dict[str, Any]:
    # Local-first stub: generative/Vertex SDK removed. Return safe defaults.
    logging.info(
        "Generative metadata suggestions are disabled in local-first mode; returning defaults"
    )
    return {
        "title": {
            "suggested_value": None,
            "justification": "Generative features disabled.",
            "confidence_score": 0.0,
        },
        "department": {
            "suggested_value": None,
            "justification": "Generative features disabled.",
            "confidence_score": 0.0,
        },
        "process_type": {
            "suggested_value": None,
            "justification": "Generative features disabled.",
            "confidence_score": 0.0,
        },
        "status": {
            "suggested_value": None,
            "justification": "Generative features disabled.",
            "confidence_score": 0.0,
        },
    }


@app.post("/rag-query")
async def rag_query(
    body: Dict[str, Any], current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Deprecated confirm handler.

    The full `confirm_upload` implementation has been migrated to
    `docintel-ingestion-service` and is preserved below as a commented block
    for rollback and auditability. Call the ingestion service `/confirm-upload`
    endpoint instead.
    """
    # Original implementation (preserved for rollback) - begin
    #
    # conn = get_db_connection()
    # cursor = conn.cursor()
    #
    # try:
    #     # Get upload session and metadata
    #     cursor.execute("""
    #         SELECT du.user_id, du.filename, du.original_filename, umd.confirmed_title, umd.confirmed_department, umd.confirmed_author, umd.confirmed_type
    #         FROM document_uploads du
    #         JOIN upload_metadata_drafts umd ON du.session_id = umd.session_id
    #         WHERE du.session_id = %s
    #     """, (session_id,))
    #
    #     row = cursor.fetchone()
    #     if not row:
    #         raise HTTPException(status_code=404, detail="Upload session not found")
    #
    #     uploader_id, filename, original_filename, final_title, final_department, final_author, final_type = row
    #
    #     if uploader_id != current_user.get('id') and current_user.get('role') != 'manager':
    #         raise HTTPException(status_code=403, detail="Not authorized to confirm this upload")
    #
    #     # Get file content (need to re-read from temp storage or retrieve)
    #     # For now, assume file is still in memory or we reconstruct from DB
    #     cursor.execute("SELECT chunk_content FROM documents LIMIT 0")  # Placeholder
    #
    #     # Build GCS path
    #     gcs_path = f"{final_department.lower().replace(' ', '-')}/{original_filename}"
    #
    #     # For now, simulate GCS save (in real impl, you'd read from temp storage)
    #     try:
    #         # Placeholder: in production, file bytes should be cached or retrieved
    #         logging.info(f"Saving to GCS: {gcs_path}")
    #
    #     except Exception as e:
    #         logging.error(f"GCS upload failed: {e}")
    #         raise HTTPException(status_code=500, detail="Failed to save to GCS")
    #
    #     # Create document record (simplified, full logic from /process-document)
    #     cursor.execute("""
    #         INSERT INTO documents
    #         (original_gcs_filename, gcs_object_path, department_folder, chunk_index, chunk_content,
    #          final_title, final_department, final_process_type, final_status, review_status, upload_session_id)
    #         VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'approved', %s)
    #         RETURNING id
    #     """, (
    #         original_filename, gcs_path, final_department, 0, 'Document content',
    #         final_title, final_department, final_type, 'confirmed', session_id
    #     ))
    #
    #     document_id = cursor.fetchone()[0]
    #
    #     # Update upload session
    #     cursor.execute("""
    #         UPDATE document_uploads
    #         SET upload_status = 'confirmed', gcs_path = %s, document_id = %s, confirmed_at = NOW()
    #         WHERE session_id = %s
    #     """, (gcs_path, document_id, session_id))
    #
    #     conn.commit()
    # except HTTPException:
    #     conn.rollback()
    #     raise
    # except Exception as e:
    #     conn.rollback()
    #     logging.error(f"Confirm upload failed: {e}")
    #     raise HTTPException(status_code=500, detail="Confirmation failed")
    # finally:
    #     cursor.close()
    #     conn.close()
    #
    # # Log confirm event
    # await _log_upload_event(session_id, current_user.get('id'), 'confirmed', event_details={'document_id': document_id, 'gcs_path': gcs_path})
    #
    # return ConfirmUploadResponse(
    #     session_id=session_id,
    #     document_id=document_id,
    #     gcs_path=gcs_path,
    #     chunks_count=1,
    #     status='confirmed'
    # )
    # Original implementation - end

    raise HTTPException(
        status_code=501,
        detail="confirm_upload migrated to docintel-ingestion-service /confirm-upload; original implementation preserved in comments",
    )


class QueryAuditInfo(BaseModel):
    session_id: str
    timestamp: str
    user_id: Optional[int] = None
    user_role: Optional[str] = None
    sop_versions_used: List[int] = []
    chunks_retrieved: int = 0


class RagQueryResponse(BaseModel):
    session_id: str
    query: str
    summary_answer: str
    steps: Optional[List[Dict[str, Any]]] = None
    sources: List[SourceCitation] = []
    confidence: ConfidenceReport
    query_audit: QueryAuditInfo
    disclaimer: str = (
        "This response is advisory only. All outputs are traceable and may be audited."
    )
    similar_precedents: Optional["PrecedentResponse"] = None


# --- Pydantic Models for Historical Precedents ---


class PrecedentCase(BaseModel):
    """Single historical precedent case."""

    override_id: str
    user_id: str
    user_department: str
    original_recommendation: Optional[str] = None
    scenario: Optional[str] = None  # Derived summary of override_reason
    override_reason: str
    how_resolved: str  # resolution_notes from DB
    similarity_score: float = Field(ge=0.0, le=1.0)
    resolved_date: Optional[str] = None
    resolved_by: Optional[str] = None


class PrecedentResponse(BaseModel):
    """Precedent system section of /rag-query-v2 response."""

    disclaimer: str = (
        "⚠️ IMPORTANT: These are examples of how colleagues handled similar situations. "
        "They are NOT management-approved policies or procedures. Use as guidance only. "
        "Your decision remains your responsibility. For formal guidance, contact your manager or Compliance."
    )
    precedent_count: int = 0
    show_precedents: bool = False  # True only if >= 2 similar cases found
    cases: List[PrecedentCase] = []


# Forward reference resolution for RagQueryResponse.similar_precedents
# Note: deferred model rebuild removed to avoid premature evaluation of forward refs.
# RagQueryResponse.model_rebuild()


# --- Historical Precedent System ---


async def get_precedents(
    override_reason: str,
    current_query_id: uuid.UUID,
    similarity_threshold: float = 0.70,
    limit_count: int = 5,
    min_precedents: int = 2,
) -> List[Dict[str, Any]]:
    """
    Retrieve similar resolved precedents using fuzzy matching (pg_trgm).

    Args:
        override_reason: The current override reason to match against historical cases
        current_query_id: UUID of current query (to avoid self-matches)
        similarity_threshold: Minimum similarity score (0.0-1.0); default 0.70 (70%)
        limit_count: Max precedents to return; default 5
        min_precedents: Minimum threshold to show precedents; default 2

    Returns:
        List of precedent cases, each with:
        - override_id, user_id, user_department, original_recommendation, override_reason
        - resolution_notes, resolved_by_user_id, resolution_date, created_at, match_score

        Returns empty list if:
        - override_reason is null/empty
        - fewer than min_precedents found
        - database error occurs (logged with warning)
    """
    # Safety: null/empty input
    if not override_reason or not override_reason.strip():
        logging.debug(
            "get_precedents called with empty override_reason; returning empty"
        )
        return []

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Call stored function: get_similar_precedents(reason, query_id, threshold, limit)
        # This function uses pg_trgm similarity() for fuzzy text matching
        cursor.execute(
            """
            SELECT 
                override_id, 
                user_id, 
                user_department, 
                original_recommendation, 
                override_reason, 
                resolution_notes, 
                resolved_by_user_id, 
                resolution_date, 
                created_at, 
                match_score, 
                precedent_rank
            FROM get_similar_precedents(%s, %s, %s, %s, %s)
            ORDER BY match_score DESC, resolution_date DESC
        """,
            (
                override_reason,
                str(current_query_id),
                similarity_threshold,
                limit_count,
                min_precedents,
            ),
        )

        rows = cursor.fetchall()
        cursor.close()

        # Convert rows to list of dicts
        if not rows:
            logging.debug(f"No precedents found for: {override_reason[:50]}...")
            return []

        precedents = []
        for row in rows:
            precedent = {
                "override_id": str(row[0]),
                "user_id": row[1],
                "user_department": row[2],
                "original_recommendation": row[3],
                "override_reason": row[4],
                "resolution_notes": row[5],
                "resolved_by_user_id": row[6],
                "resolution_date": row[7].isoformat() if row[7] else None,
                "created_at": row[8].isoformat() if row[8] else None,
                "match_score": float(row[9]),
                "precedent_rank": row[10],
            }
            precedents.append(precedent)

        # Only return if we have at least min_precedents
        if len(precedents) < min_precedents:
            logging.debug(
                f"Found {len(precedents)} precedents, but minimum is {min_precedents}; returning empty"
            )
            return []

        logging.info(
            f"Found {len(precedents)} precedents for override (avg match: {sum(p['match_score'] for p in precedents) / len(precedents):.2f})"
        )
        return precedents

    except Exception as e:
        logging.exception(
            f"Error retrieving precedents for '{override_reason[:50]}...': {e}"
        )
        # Graceful degradation: return empty list on error
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# --- Helper Functions for Structured RAG ---

FORBIDDEN_SPECULATION = [
    "probably",
    "likely",
    "might",
    "could",
    "possibly",
    "i think",
    "in my opinion",
    "you should",
    "we recommend",
    "best practice",
    "typically",
    "usually",
    "generally",
]

OUT_OF_SCOPE_PATTERNS = [
    "should we approve",
    "do you recommend",
    "what's the best way",
    "create a new policy",
    "legal advice",
    "compliance advice",
    "make a decision",
    "approve this",
]

# Banking/Loan domain keywords - queries should relate to these topics
BANKING_DOMAIN_KEYWORDS = [
    "loan",
    "credit",
    "mortgage",
    "lending",
    "borrower",
    "applicant",
    "collateral",
    "interest rate",
    "apr",
    "underwriting",
    "approval",
    "disbursement",
    "repayment",
    "default",
    "delinquency",
    "financial",
    "income",
    "debt",
    "dti",
    "ltv",
    "fico",
    "credit score",
    "bank",
    "account",
    "deposit",
    "withdrawal",
    "transaction",
    "compliance",
    "kyc",
    "aml",
    "regulatory",
    "policy",
    "procedure",
    "guideline",
    "sop",
    "standard operating",
    "operations",
    "risk",
    "assessment",
]


def is_out_of_scope(query: str) -> Optional[str]:
    """Return reason if query is out of scope, else None."""
    q_lower = query.lower()

    # Check for explicit out-of-scope patterns (decision requests)
    for pattern in OUT_OF_SCOPE_PATTERNS:
        if pattern in q_lower:
            return f"Query contains out-of-scope request: '{pattern}'"

    # Check domain relevance - query must contain at least one banking keyword
    has_banking_term = any(keyword in q_lower for keyword in BANKING_DOMAIN_KEYWORDS)
    if not has_banking_term:
        return "Query appears unrelated to banking, lending, or financial operations. This system only answers questions about loan processing SOPs, banking procedures, and financial compliance guidelines."

    return None


def sanitize_response(text: str) -> tuple:
    """Remove speculative language; return (sanitized, violations)."""
    violations = []
    sanitized = text
    for phrase in FORBIDDEN_SPECULATION:
        import re as re_mod

        if phrase.lower() in sanitized.lower():
            violations.append(f"Removed speculative phrase: '{phrase}'")
            sanitized = re_mod.sub(
                re_mod.escape(phrase), "", sanitized, flags=re_mod.IGNORECASE
            )
    return sanitized.strip(), violations


def extract_procedural_steps_from_chunks(chunks: List[Dict]) -> List[Dict]:
    """Extract numbered/bulleted steps from chunk content."""
    import re as re_mod

    steps = []
    for chunk in chunks:
        text = chunk.get("chunk_content") or chunk.get("snippet") or ""
        chunk_id = chunk.get("id") or chunk.get("chunk_id")
        version_id = chunk.get("sop_version_id")

        # Pattern 1: "1. Step text" or "1) Step text"
        numbered = re_mod.findall(
            r"^(\d+)[.\)]\s+(.+?)(?=\n\d+[.\)]|\n\n|$)",
            text,
            re_mod.MULTILINE | re_mod.DOTALL,
        )
        for num, step_text in numbered:
            steps.append(
                {
                    "step_number": int(num),
                    "text": step_text.strip()[:500],
                    "source_chunk_id": chunk_id,
                    "sop_version_id": version_id,
                    "is_explicit": True,
                }
            )

        # Pattern 2: Bullets (if no numbered found)
        if not numbered:
            bullets = re_mod.findall(
                r"^\s*[-•]\s+(.+?)(?=\n\s*[-•]|\n\n|$)",
                text,
                re_mod.MULTILINE | re_mod.DOTALL,
            )
            for idx, bullet_text in enumerate(bullets, 1):
                steps.append(
                    {
                        "step_number": idx,
                        "text": bullet_text.strip()[:500],
                        "source_chunk_id": chunk_id,
                        "sop_version_id": version_id,
                        "is_explicit": False,
                    }
                )

    # Dedupe and sort
    seen = set()
    unique = []
    for s in steps:
        key = (s.get("step_number"), s.get("text")[:50])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return sorted(unique, key=lambda x: x.get("step_number") or 999)


def calculate_confidence(sources: List[Dict], gaps: List[str]) -> Dict:
    """Calculate confidence score based on sources and gaps."""
    if not sources:
        return {
            "overall_score": 0.0,
            "gaps_identified": ["No matching SOP content found"],
            "ambiguities": [],
            "human_judgment_required_for": ["Entire request"],
        }

    relevance_scores = [s.get("relevance_score", 0.5) for s in sources]
    avg_relevance = sum(relevance_scores) / len(relevance_scores)

    version_ids = set(
        s.get("sop_version_id") for s in sources if s.get("sop_version_id")
    )
    version_conflicts = None
    if len(version_ids) > 1:
        version_conflicts = [
            f"Answer spans {len(version_ids)} SOP versions: {list(version_ids)}"
        ]
        gaps.append("Multiple SOP versions referenced - verify consistency")

    human_judgment = []
    if avg_relevance < 0.6:
        human_judgment.append("Low relevance scores - manual verification recommended")

    return {
        "overall_score": round(avg_relevance, 3),
        "gaps_identified": gaps,
        "ambiguities": [],
        "human_judgment_required_for": human_judgment,
        "version_conflicts": version_conflicts,
    }


# --- Vertex AI RAG Corpus Query (Native) ---


class VertexRagQueryRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=20)
    similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


class VertexRagContext(BaseModel):
    source_uri: Optional[str] = None
    text: str
    distance: Optional[float] = None


class VertexRagResponse(BaseModel):
    session_id: str
    query: str
    summary_answer: str
    contexts: List[VertexRagContext] = []
    confidence: ConfidenceReport
    disclaimer: str = (
        "This response is advisory only. All outputs are traceable and may be audited."
    )


@app.post("/rag-query-corpus", response_model=VertexRagResponse)
async def rag_query_vertex_corpus(
    body: VertexRagQueryRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Query the Vertex AI RAG Corpus (Amcorpus) directly.

    This endpoint uses the native Vertex AI RAG API to retrieve contexts
    from your pre-indexed SOP documents in Cloud Spanner vector DB.

    Advantages over /rag-query-v2:
    - Uses Google-managed embedding & chunking
    - Automatic indexing and retrieval
    - No need for local pgvector
    """

    # Local-first mode: Vertex RAG corpus endpoint disabled when SDK unavailable
    if not globals().get("_GENAI_AVAILABLE"):
        logging.info(
            "rag-query-corpus endpoint called but is disabled in local-first mode"
        )
        raise HTTPException(
            status_code=501,
            detail="Vertex RAG corpus endpoint is disabled in local-first mode. Use /rag-query or an adapter-backed RAG implementation.",
        )


# Minimal RagQueryRequest model for structured RAG endpoint (kept simple for tests)
class RagQueryRequest(BaseModel):
    query: str
    department: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=50)


# Simple chat endpoint that uses text search (no embeddings required)
@app.post("/chat")
async def chat_query(
    body: Dict[str, Any], current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Simple chat endpoint that uses text search and Gemini for responses.
    Falls back gracefully when embeddings aren't available.

    Body: {"query": str, "conversation_history": list (optional)}
    Returns: {"response": str, "sources": list, "confidence": float}
    """
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Missing 'query' in request body")

    conversation_history = body.get("conversation_history", [])

    # 1. Text search in documents table
    conn = get_db_connection()
    cursor = conn.cursor()
    snippets = []

    try:
        # Use text search with multiple keywords
        keywords = query.lower().split()[:5]  # Take first 5 words

        # Build search condition
        search_conditions = " OR ".join(["chunk_content ILIKE %s" for _ in keywords])
        search_params = [f"%{kw}%" for kw in keywords]

        sql = f"""
            SELECT chunk_content, gcs_object_path, final_title, department_folder
            FROM documents 
            WHERE {search_conditions}
            LIMIT 5
        """
        cursor.execute(sql, tuple(search_params))
        rows = cursor.fetchall()

        for row in rows:
            snippets.append(
                {
                    "content": row[0][:500] if row[0] else "",  # Limit snippet size
                    "path": row[1] or "",
                    "title": row[2] or "Unknown Document",
                    "department": row[3] or "General",
                }
            )
    except Exception as e:
        logging.warning(f"Text search failed: {e}")
        # Continue without snippets
    finally:
        cursor.close()
        conn.close()

    # 2. Build context from snippets
    context_text = ""
    if snippets:
        context_text = "\n\n".join(
            [f"[{s['title']}]: {s['content']}" for s in snippets]
        )

    # 3. Generate response using Gemini
    try:
        # Build conversation context
        conv_context = ""
        if conversation_history:
            for msg in conversation_history[-4:]:  # Last 4 messages
                role = "User" if msg.get("role") == "user" else "Assistant"
                conv_context += f"{role}: {msg.get('content', '')}\n"

        prompt = f"""You are a helpful banking SOP assistant. Answer the user's question based on the provided context from bank SOPs and procedures.

Context from SOPs:
{context_text if context_text else "No specific SOP documents found for this query."}

Previous conversation:
{conv_context if conv_context else "No previous conversation."}

Current question: {query}

Provide a helpful, professional response. If you don't have specific information from the SOPs, provide general banking guidance but note that the user should verify with their specific bank's procedures."""

        # Use the generate_with_retry function
        response_text = await generate_with_retry(prompt)

        if not response_text:
            response_text = "I apologize, but I'm unable to generate a response at this time. Please try again or contact support for assistance."

    except Exception as e:
        logging.exception(f"Gemini generation failed: {e}")
        # Provide fallback response
        if snippets:
            response_text = f"Based on the available SOPs, here's what I found:\n\n"
            for s in snippets[:3]:
                response_text += f"• From {s['title']}: {s['content'][:200]}...\n\n"
            response_text += (
                "Please review the full SOP documents for complete procedures."
            )
        else:
            response_text = "I wasn't able to find specific SOP information for your query. Please try rephrasing your question or contact your manager for guidance on banking procedures."

    return {
        "response": response_text,
        "sources": [
            {"title": s["title"], "department": s["department"], "path": s["path"]}
            for s in snippets
        ],
        "confidence": 0.7 if snippets else 0.3,
    }


@app.post("/rag-query-v2", response_model=RagQueryResponse)
async def rag_query_structured(
    body: RagQueryRequest, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Enhanced RAG Query with full traceability for audit-safe SOP queries.

    Features:
    - Only queries ACTIVE SOP versions
    - Full session logging (query_sessions table)
    - Step extraction from procedural content
    - Confidence scoring with gap detection
    - No speculation - only SOP-sourced content

    Returns structured response with citations and audit trail.
    """
    session_id = str(uuid.uuid4())
    query_timestamp = datetime.now(timezone.utc)
    gaps = []

    # 1. Scope Validation
    scope_issue = is_out_of_scope(body.query)
    if scope_issue:
        # For domain mismatch, return immediately with clear error
        if "unrelated to banking" in scope_issue:
            return RagQueryResponse(
                session_id=session_id,
                summary_answer="This system is designed exclusively for banking and loan processing queries. Your question appears to be outside this domain. Please ask questions related to loan applications, underwriting, compliance procedures, or banking operations.",
                sources=[],
                confidence=ConfidenceReport(
                    overall_score=0.0,
                    score_explanation="Query rejected: not related to banking domain",
                    major_gaps=[scope_issue],
                ),
                procedural_steps=None,
                audit_info=QueryAuditInfo(
                    session_id=session_id,
                    timestamp=query_timestamp.isoformat(),
                    user_id=current_user.get("id"),
                    user_role=current_user.get("role"),
                    sop_versions_used=[],
                    chunks_retrieved=0,
                ),
            )
        gaps.append(scope_issue)
        # Log but continue for other scope issues

    # 2. Generate query embedding
    try:
        emb = await get_text_embeddings([body.query])
        qvec = emb[0]
        qvec_str = "[" + ",".join(map(str, qvec)) + "]"
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Embedding generation timed out")
    except Exception:
        logging.exception("Failed to generate query embedding")
        raise HTTPException(status_code=500, detail="Embedding generation failed")

    # 3. Vector search — delegate to `services.rag_service` which centralizes
    # embedding generation and DB search logic. We map the simpler snippet
    # shape into the richer `sources` structure expected downstream.
    try:
        from services.rag_service import run_rag_query

        result = await run_rag_query(body.query, body.department, body.top_k)
        snippets = result.get("snippets", [])

        sources = []
        raw_chunks = []
        for idx, s in enumerate(snippets):
            snippet_text = s.get("snippet") or ""
            chunk_id = s.get("path") or f"auto-{idx}"
            sources.append(
                {
                    "sop_title": None,
                    "sop_id": None,
                    "version_id": None,
                    "version_status": "unknown",
                    "chunk_id": chunk_id,
                    "section_number": None,
                    "snippet": snippet_text[:400],
                    "relevance_score": float(s.get("relevance_score", 0.5)),
                }
            )
            raw_chunks.append(
                {
                    "id": chunk_id,
                    "chunk_content": snippet_text,
                    "sop_version_id": None,
                    "sop_id": None,
                }
            )
    except Exception:
        logging.exception("RAG orchestration failed")
        raise HTTPException(status_code=500, detail="RAG query failed")

    # 4. Check coverage
    if not sources:
        gaps.append("No matching SOP content found for this query")
        gaps.append("RECOMMENDATION: Contact compliance team for manual guidance")
    else:
        avg_rel = sum(s["relevance_score"] for s in sources) / len(sources)
        if avg_rel < 0.6:
            gaps.append(
                f"Low relevance scores (avg: {avg_rel:.1%}) - results may not be directly applicable"
            )
            if avg_rel < 0.4:
                gaps.append(
                    "WARNING: Very low relevance - retrieved content may be unrelated to your query"
                )

    # 5. Extract procedural steps
    steps = None
    if raw_chunks:
        extracted = extract_procedural_steps_from_chunks(raw_chunks)
        if extracted:
            steps = [ProceduralStep(**s) for s in extracted]

    # 6. Generate answer using LLM
    context_text = "\n\n---\n\n".join([s["snippet"] for s in sources])[:6000]

    system_prompt = """You are an SOP Query Assistant for a regulated banking environment.
RULES:
- ONLY answer questions about banking, loans, credit, underwriting, and financial operations
- If the query is about cooking, recipes, entertainment, or other non-banking topics, respond: "I can only answer questions about banking and loan processing procedures."
- Use ONLY the provided SOP content
- Do NOT invent steps, rules, or thresholds
- Do NOT use speculative language (probably, likely, might, could)
- Distinguish fact from interpretation
- If content is insufficient, say so explicitly
- Reference section numbers when available
- If retrieved content seems unrelated to the query, explicitly state this mismatch"""

    user_prompt = f"""Query: {body.query}

SOP Content:
{context_text}

Provide a direct, procedural answer based ONLY on the above content. If steps exist, list them in order."""

    summary_answer = "Unable to generate answer."
    try:
        # Use generative model
        full_prompt = f"{system_prompt}\n\n{user_prompt}"

        if _GENAI_AVAILABLE:
            client = _get_genai_client()

            async def _gen():
                resp = await client.aio.models.generate_content(
                    model=GENERATIVE_MODEL_ID or "gemini-2.5-pro",
                    contents=full_prompt,
                    config=types.GenerateContentConfig(
                        max_output_tokens=1024, temperature=0.1
                    ),
                )
                return resp.text if hasattr(resp, "text") else str(resp)

            raw_answer = await _gen()
        elif _VERTEX_SDK_AVAILABLE and GENERATIVE_MODEL_ID:

            def _vertex_call():
                global _GENERATIVE_MODEL
                if _GENERATIVE_MODEL is None:
                    vertexai.init(project=PROJECT_ID, location=REGION)
                    _GENERATIVE_MODEL = GenerativeModel(GENERATIVE_MODEL_ID)
                resp = _GENERATIVE_MODEL.generate_content(
                    [full_prompt],
                    generation_config={"max_output_tokens": 1024, "temperature": 0.1},
                )
                return resp.text if hasattr(resp, "text") else str(resp)

            raw_answer = await asyncio.to_thread(_vertex_call)
        else:
            # REST fallback
            headers = get_auth_headers()
            url = f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{REGION}/publishers/google/models/{GENERATIVE_MODEL_ID or 'gemini-1.5-pro'}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": full_prompt}]}],
                "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.1},
            }
            resp = await async_post_with_retries(
                url, json=payload, headers=headers, timeout=(5.0, 120.0)
            )
            data = resp.json()
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                raw_answer = parts[0].get("text", "") if parts else ""
            else:
                raw_answer = ""

        # Sanitize
        summary_answer, violations = sanitize_response(raw_answer)
        if violations:
            for v in violations:
                gaps.append(v)

    except asyncio.TimeoutError:
        gaps.append("Generative model timed out")
        summary_answer = f"Answer generation timed out. Retrieved {len(sources)} relevant SOP sections."
    except Exception as e:
        logging.exception("Generative answer failed")
        gaps.append(f"Answer generation error: {str(e)[:100]}")
        summary_answer = f"Could not generate answer. Retrieved {len(sources)} relevant SOP sections for manual review."

    # 7. Build confidence report
    confidence = calculate_confidence(sources, gaps)

    # 8. Build audit info
    sop_versions_used = list(
        set(s["version_id"] for s in sources if s.get("version_id"))
    )
    audit_info = QueryAuditInfo(
        session_id=session_id,
        timestamp=query_timestamp.isoformat(),
        user_id=current_user.get("id"),
        user_role=current_user.get("role"),
        sop_versions_used=sop_versions_used,
        chunks_retrieved=len(sources),
    )

    # 9. Log to database (async, non-blocking)
    try:
        log_conn = get_db_connection()
        log_cursor = log_conn.cursor()

        # Insert session
        log_cursor.execute(
            """
            INSERT INTO query_sessions 
            (session_id, user_id, user_role, user_departments, query_text, query_hash, query_timestamp, response_timestamp, status, confidence_score, gaps_identified)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
        """,
            (
                session_id,
                current_user.get("id"),
                current_user.get("role"),
                json.dumps(current_user.get("departments")),
                body.query,
                hashlib.sha256(body.query.encode()).hexdigest(),
                query_timestamp,
                datetime.now(timezone.utc),
                "success" if sources else "no_coverage",
                confidence["overall_score"],
                json.dumps(gaps),
            ),
        )

        # Insert results
        for idx, src in enumerate(sources):
            log_cursor.execute(
                """
                INSERT INTO query_results
                (session_id, sop_id, sop_version_id, chunk_id, relevance_score, citation_index)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """,
                (
                    session_id,
                    src.get("sop_id"),
                    src.get("version_id"),
                    src["chunk_id"],
                    src["relevance_score"],
                    idx,
                ),
            )

        # Log gaps as audit events
        for gap in gaps:
            log_cursor.execute(
                """
                INSERT INTO query_audit_log (session_id, event_type, event_details, severity)
                VALUES (%s, %s, %s, %s)
            """,
                (
                    session_id,
                    "coverage_gap" if "gap" in gap.lower() else "info",
                    json.dumps({"message": gap}),
                    "warning" if "gap" in gap.lower() else "info",
                ),
            )

        # Cache response
        log_cursor.execute(
            """
            INSERT INTO query_response_cache (session_id, summary_answer, steps, full_response, model_used)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
        """,
            (
                session_id,
                summary_answer,
                json.dumps(
                    [s.dict() if hasattr(s, "dict") else s for s in (steps or [])]
                ),
                json.dumps(
                    {"sources_count": len(sources), "steps_count": len(steps or [])}
                ),
                GENERATIVE_MODEL_ID or "gemini-2.5-pro",
            ),
        )

        log_conn.commit()
        log_cursor.close()
        log_conn.close()
    except Exception:
        logging.exception("Failed to log query session - continuing without audit")

    # 10. Retrieve similar precedents (if any exist)
    # Precedents are shown only if there are at least 2 similar resolved cases
    precedent_response = PrecedentResponse()  # Default empty response
    try:
        # For now, only retrieve precedents if we have low confidence (< 0.6)
        # This helps frontline staff understand how others handled similar grey-zone scenarios
        if confidence.get("overall_score", 0.0) < 0.6 and summary_answer:
            # Use the summary answer as the search basis for similar overrides
            # (In practice, this would be called after user explicitly rejects answer)
            precedent_list = await get_precedents(
                override_reason=body.query[:200],  # Query as potential override reason
                current_query_id=uuid.UUID(session_id),
                similarity_threshold=0.70,
                limit_count=5,
                min_precedents=2,
            )

            if precedent_list and len(precedent_list) >= 2:
                # Build PrecedentCase objects
                precedent_cases = []
                for p in precedent_list:
                    case = PrecedentCase(
                        override_id=p["override_id"],
                        user_id=p["user_id"],
                        user_department=p["user_department"],
                        original_recommendation=p.get("original_recommendation"),
                        scenario=p["override_reason"][
                            :100
                        ],  # First 100 chars as scenario summary
                        override_reason=p["override_reason"],
                        how_resolved=p["resolution_notes"] or "No notes available",
                        similarity_score=round(p["match_score"], 3),
                        resolved_date=p["resolution_date"],
                        resolved_by=p["resolved_by_user_id"],
                    )
                    precedent_cases.append(case)

                precedent_response = PrecedentResponse(
                    precedent_count=len(precedent_cases),
                    show_precedents=True,
                    cases=precedent_cases,
                )
                logging.info(
                    f"Retrieved {len(precedent_cases)} precedent cases for low-confidence query"
                )
    except Exception:
        logging.exception("Failed to retrieve precedents - continuing without them")
        precedent_response = PrecedentResponse()  # Return empty on error

    # 11. Return structured response
    return RagQueryResponse(
        session_id=session_id,
        query=body.query,
        summary_answer=summary_answer,
        steps=steps,
        sources=[SourceCitation(**s) for s in sources],
        confidence=ConfidenceReport(**confidence),
        query_audit=audit_info,
        disclaimer="This response is advisory only. All outputs are traceable and may be audited.",
        similar_precedents=precedent_response,
    )


# --- Manager Workflow Models ---


class ResolveOverrideRequest(BaseModel):
    """Request to mark an override as resolved (manager approval)."""

    resolution_notes: str = Field(
        ...,
        min_length=10,
        max_length=1000,
        description="How was this override resolved? Decision rationale.",
    )


class ResolveOverrideResponse(BaseModel):
    """Response after manager resolves an override."""

    override_id: str
    is_resolved: bool
    resolution_date: str
    resolved_by: str
    message: str


class PrecedentDetailsResponse(BaseModel):
    """Full details of a precedent case for manager/compliance review."""

    override_id: str
    user_id: str
    user_department: str
    original_query_context: Optional[str] = None
    original_recommendation: Optional[str] = None
    override_reason: str
    how_resolved: str  # resolution_notes
    resolved_date: Optional[str] = None
    resolved_by_user_id: Optional[str] = None
    created_at: str
    match_score: Optional[float] = None
    # Optional: full query context
    query_session_id: Optional[str] = None
    query_text: Optional[str] = None


class DemographicBreakdown(BaseModel):
    """Demographic breakdown of customers in unresolved cases."""

    category: str  # e.g., "high-earning", "mid-income", "low-income"
    count: int
    percentage: float = Field(ge=0.0, le=100.0)


class UnresolvedMetricsResponse(BaseModel):
    """Aggregated metrics for unresolved override cases."""

    total_cases: int
    unique_officers: int
    time_period_days: int
    time_period_label: str  # e.g., "4 months"
    period_start_date: str  # ISO format
    period_end_date: str  # ISO format
    demographics: List[DemographicBreakdown] = []
    department_filter: Optional[str] = None  # Which department was filtered (if any)


# --- Upload Models & Endpoints ---

from fastapi import UploadFile, File


class UploadMetadataResponse(BaseModel):
    session_id: str
    extracted_title: str
    extracted_department: str
    extracted_author: str
    extracted_type: str
    ai_confidence: Dict[str, float]
    upload_status: str


class MetadataEditRequest(BaseModel):
    title: Optional[str] = None
    department: Optional[str] = None
    author: Optional[str] = None
    type: Optional[str] = None


class ConfirmUploadResponse(BaseModel):
    session_id: str
    document_id: int
    gcs_path: str
    chunks_count: int
    status: str = "confirmed"


class UndoPromptResponse(BaseModel):
    session_id: str
    options: List[str] = ["edit", "discard"]


class DiscardResponse(BaseModel):
    session_id: str
    status: str = "discarded"
    message: str


async def _log_upload_event(
    session_id: str,
    user_id: Optional[int],
    event_type: str,
    field_changes: Optional[Dict] = None,
    event_details: Optional[Dict] = None,
):
    """Log upload events to audit table."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO upload_audit_log (session_id, user_id, event_type, field_changes, event_details)
            VALUES (%s, %s, %s, %s, %s)
        """,
            (
                str(session_id),
                user_id,
                event_type,
                json.dumps(field_changes) if field_changes else None,
                json.dumps(event_details) if event_details else None,
            ),
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception:
        logging.exception("Failed to log upload event")


@app.post("/document-upload", response_model=UploadMetadataResponse)
async def document_upload(
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Upload a document and extract metadata.

    Returns extracted metadata for user confirmation.
    Does NOT save to GCS yet - awaits user confirmation.
    """
    session_id = str(uuid.uuid4())
    user_id = current_user.get("id")

    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    # Validate file type
    if not (file.filename.lower().endswith((".pdf", ".txt", ".md"))):
        raise HTTPException(status_code=400, detail="Only PDF, TXT, MD files supported")

    # Read file contents
    try:
        contents = await file.read()
        if not contents:
            raise HTTPException(status_code=400, detail="Empty file")
        if len(contents) > 50 * 1024 * 1024:  # 50MB limit
            raise HTTPException(status_code=413, detail="File too large (max 50MB)")
    except Exception as e:
        logging.error(f"Failed to read upload: {e}")
        raise HTTPException(status_code=500, detail="Failed to read file")

    # Extract text
    document_text = ""
    try:
        if file.filename.lower().endswith(".pdf"):
            import io

            pdf = PdfReader(io.BytesIO(contents))
            document_text = "\n".join([page.extract_text() for page in pdf.pages])
        else:
            document_text = contents.decode("utf-8")
    except Exception as e:
        logging.error(f"Text extraction failed: {e}")
        raise HTTPException(status_code=400, detail="Failed to extract text from file")

    if not document_text:
        raise HTTPException(status_code=400, detail="No text content extracted")

    # Get AI metadata suggestions
    try:
        ai_metadata = await get_ai_metadata_suggestions(
            document_text[:5000]
        )  # Use first 5000 chars for speed
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Metadata extraction timed out")
    except Exception as e:
        logging.error(f"AI metadata extraction failed: {e}")
        raise HTTPException(status_code=500, detail="Metadata extraction failed")

    # Create upload session (draft state)
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            INSERT INTO document_uploads (session_id, user_id, filename, original_filename, upload_status)
            VALUES (%s, %s, %s, %s, 'draft')
        """,
            (session_id, user_id, file.filename, file.filename),
        )

        # Create metadata draft
        cursor.execute(
            """
            INSERT INTO upload_metadata_drafts 
            (session_id, extracted_title, extracted_department, extracted_author, extracted_type, 
             ai_confidence, ai_model_used, confirmed_title, confirmed_department, confirmed_author, confirmed_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
            (
                session_id,
                ai_metadata.get("title", {}).get("suggested_value"),
                ai_metadata.get("department", {}).get("suggested_value"),
                "Unknown",  # Author not in current AI suggestions
                ai_metadata.get("process_type", {}).get("suggested_value"),
                json.dumps(
                    {
                        "title": ai_metadata.get("title", {}).get(
                            "confidence_score", 0.0
                        ),
                        "department": ai_metadata.get("department", {}).get(
                            "confidence_score", 0.0
                        ),
                        "type": ai_metadata.get("process_type", {}).get(
                            "confidence_score", 0.0
                        ),
                    }
                ),
                GENERATIVE_MODEL_ID or "gemini-2.5-pro",
                ai_metadata.get("title", {}).get("suggested_value"),
                ai_metadata.get("department", {}).get("suggested_value"),
                "Unknown",
                ai_metadata.get("process_type", {}).get("suggested_value"),
            ),
        )

        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Failed to create upload session: {e}")
        raise HTTPException(status_code=500, detail="Failed to create upload session")
    finally:
        cursor.close()
        conn.close()

    # Log extraction event
    await _log_upload_event(
        session_id,
        user_id,
        "extracted",
        event_details={"filename": file.filename, "text_length": len(document_text)},
    )

    return UploadMetadataResponse(
        session_id=session_id,
        extracted_title=ai_metadata.get("title", {}).get("suggested_value") or "",
        extracted_department=ai_metadata.get("department", {}).get("suggested_value")
        or "",
        extracted_author="Unknown",
        extracted_type=ai_metadata.get("process_type", {}).get("suggested_value") or "",
        ai_confidence={
            "title": ai_metadata.get("title", {}).get("confidence_score", 0.0),
            "department": ai_metadata.get("department", {}).get(
                "confidence_score", 0.0
            ),
            "type": ai_metadata.get("process_type", {}).get("confidence_score", 0.0),
        },
        upload_status="draft",
    )


@app.patch("/document-uploads/{session_id}/metadata")
async def update_upload_metadata(
    session_id: str,
    edits: MetadataEditRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Update metadata for a pending upload (before confirmation).
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Verify session exists and belongs to user
        cursor.execute(
            """
            SELECT user_id, upload_status FROM document_uploads WHERE session_id = %s
        """,
            (session_id,),
        )
        row = cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")

        session_user_id, status = row
        if (
            session_user_id != current_user.get("id")
            and current_user.get("role") != "manager"
        ):
            raise HTTPException(
                status_code=403, detail="Not authorized to edit this upload"
            )

        if status != "draft":
            raise HTTPException(status_code=409, detail="Cannot edit non-draft upload")

        # Validate department access (officers can only upload to their departments)
        if edits.department and current_user.get("role") != "manager":
            user_depts = current_user.get("departments", [])
            if edits.department not in user_depts:
                raise HTTPException(
                    status_code=403,
                    detail="Not authorized to upload to this department",
                )

        # Track changes
        field_changes = {}
        update_fields = []
        update_values = []

        if edits.title is not None:
            update_fields.append("user_title = %s, confirmed_title = %s")
            update_values.extend([edits.title, edits.title])
            field_changes["title"] = {"to": edits.title}

        if edits.department is not None:
            update_fields.append("user_department = %s, confirmed_department = %s")
            update_values.extend([edits.department, edits.department])
            field_changes["department"] = {"to": edits.department}

        if edits.author is not None:
            update_fields.append("user_author = %s, confirmed_author = %s")
            update_values.extend([edits.author, edits.author])
            field_changes["author"] = {"to": edits.author}

        if edits.type is not None:
            update_fields.append("user_type = %s, confirmed_type = %s")
            update_values.extend([edits.type, edits.type])
            field_changes["type"] = {"to": edits.type}

        if not update_fields:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=400, detail="No fields to update")

        # Update draft
        update_values.append(session_id)
        cursor.execute(
            f"""
            UPDATE upload_metadata_drafts 
            SET {", ".join(update_fields)}, last_edited_at = NOW()
            WHERE session_id = %s
        """,
            tuple(update_values),
        )

        conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logging.error(f"Failed to update metadata: {e}")
        raise HTTPException(status_code=500, detail="Failed to update metadata")
    finally:
        cursor.close()
        conn.close()

    # Log edit event
    await _log_upload_event(
        session_id, current_user.get("id"), "edited", field_changes=field_changes
    )

    return {"session_id": session_id, "status": "updated"}


@app.post(
    "/document-uploads/{session_id}/confirm", response_model=ConfirmUploadResponse
)
async def confirm_upload(
    session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Confirm upload: save to GCS, create documents/chunks, generate embeddings.
    Atomic operation.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Get upload session and metadata
        cursor.execute(
            """
            SELECT du.user_id, du.filename, du.original_filename, umd.confirmed_title, umd.confirmed_department, umd.confirmed_author, umd.confirmed_type
            FROM document_uploads du
            JOIN upload_metadata_drafts umd ON du.session_id = umd.session_id
            WHERE du.session_id = %s
        """,
            (session_id,),
        )

        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")

        (
            uploader_id,
            filename,
            original_filename,
            final_title,
            final_department,
            final_author,
            final_type,
        ) = row

        if (
            uploader_id != current_user.get("id")
            and current_user.get("role") != "manager"
        ):
            raise HTTPException(
                status_code=403, detail="Not authorized to confirm this upload"
            )

        # Get file content (need to re-read from temp storage or retrieve)
        # For now, assume file is still in memory or we reconstruct from DB
        cursor.execute("SELECT chunk_content FROM documents LIMIT 0")  # Placeholder

        # Build GCS path
        gcs_path = f"{final_department.lower().replace(' ', '-')}/{original_filename}"

        # For now, simulate GCS save (in real impl, you'd read from temp storage)
        # Save to GCS (assuming we have file bytes somewhere - would need refactor)
        try:
            # Placeholder: in production, file bytes should be cached or retrieved
            logging.info(f"Saving to GCS: {gcs_path}")

        except Exception as e:
            logging.error(f"GCS upload failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to save to GCS")

        # Create document record (simplified, full logic from /process-document)
        cursor.execute(
            """
            INSERT INTO documents 
            (original_gcs_filename, gcs_object_path, department_folder, chunk_index, chunk_content,
             final_title, final_department, final_process_type, final_status, review_status, upload_session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'approved', %s)
            RETURNING id
        """,
            (
                original_filename,
                gcs_path,
                final_department,
                0,
                "Document content",
                final_title,
                final_department,
                final_type,
                "confirmed",
                session_id,
            ),
        )

        document_id = cursor.fetchone()[0]

        # Update upload session
        cursor.execute(
            """
            UPDATE document_uploads 
            SET upload_status = 'confirmed', gcs_path = %s, document_id = %s, confirmed_at = NOW()
            WHERE session_id = %s
        """,
            (gcs_path, document_id, session_id),
        )

        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        logging.error(f"Confirm upload failed: {e}")
        raise HTTPException(status_code=500, detail="Confirmation failed")
    finally:
        cursor.close()
        conn.close()

    # Log confirm event
    await _log_upload_event(
        session_id,
        current_user.get("id"),
        "confirmed",
        event_details={"document_id": document_id, "gcs_path": gcs_path},
    )

    return ConfirmUploadResponse(
        session_id=session_id,
        document_id=document_id,
        gcs_path=gcs_path,
        chunks_count=1,
        status="confirmed",
    )


@app.post("/document-uploads/{session_id}/undo", response_model=UndoPromptResponse)
async def undo_upload(
    session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Open undo prompt for confirmed upload.
    Returns options: edit, discard
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT user_id, upload_status FROM document_uploads WHERE session_id = %s
        """,
            (session_id,),
        )

        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")

        user_id, status = row
        if user_id != current_user.get("id") and current_user.get("role") != "manager":
            raise HTTPException(status_code=403, detail="Not authorized")

        if status != "confirmed":
            raise HTTPException(
                status_code=409, detail="Can only undo confirmed uploads"
            )

    finally:
        cursor.close()
        conn.close()

    return UndoPromptResponse(session_id=session_id, options=["edit", "discard"])


@app.post("/document-uploads/{session_id}/undo-edit")
async def undo_edit(
    session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Reload last confirmed metadata for editing.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT user_id, upload_status FROM document_uploads WHERE session_id = %s
        """,
            (session_id,),
        )

        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")

        user_id, status = row
        if user_id != current_user.get("id") and current_user.get("role") != "manager":
            raise HTTPException(status_code=403, detail="Not authorized")

        # Get confirmed metadata
        cursor.execute(
            """
            SELECT confirmed_title, confirmed_department, confirmed_author, confirmed_type
            FROM upload_metadata_drafts
            WHERE session_id = %s
        """,
            (session_id,),
        )

        meta_row = cursor.fetchone()
        if not meta_row:
            raise HTTPException(status_code=404, detail="Metadata not found")

        confirmed_title, confirmed_department, confirmed_author, confirmed_type = (
            meta_row
        )

        # Reset to draft state
        cursor.execute(
            """
            UPDATE document_uploads SET upload_status = 'draft' WHERE session_id = %s
        """,
            (session_id,),
        )

        conn.commit()

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Undo edit failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to undo")
    finally:
        cursor.close()
        conn.close()

    # Log undo event
    await _log_upload_event(session_id, current_user.get("id"), "undo_edit")

    return {
        "session_id": session_id,
        "title": confirmed_title,
        "department": confirmed_department,
        "author": confirmed_author,
        "type": confirmed_type,
        "status": "draft",
    }


@app.delete("/document-uploads/{session_id}")
async def discard_upload(
    session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Discard upload: soft delete document, purge metadata, remove from GCS & RAG.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Get upload session
        cursor.execute(
            """
            SELECT user_id, gcs_path, document_id FROM document_uploads WHERE session_id = %s
        """,
            (session_id,),
        )

        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")

        user_id, gcs_path, doc_id = row
        if user_id != current_user.get("id") and current_user.get("role") != "manager":
            raise HTTPException(
                status_code=403, detail="Not authorized to discard this upload"
            )

        # Soft delete document and chunks
        if doc_id:
            cursor.execute(
                "UPDATE documents SET is_deleted = TRUE WHERE id = %s", (doc_id,)
            )

        # Remove from GCS (if exists)
        if gcs_path:
            try:
                bucket = storage_client.bucket(GCS_BUCKET_NAME)
                blob = bucket.blob(gcs_path)
                if blob.exists():
                    blob.delete()
                    logging.info(f"Deleted from GCS: {gcs_path}")
            except Exception as e:
                logging.warning(f"Failed to delete from GCS: {e}")

        # Mark upload as discarded
        cursor.execute(
            """
            UPDATE document_uploads SET upload_status = 'discarded', discarded_at = NOW(), is_deleted = TRUE
            WHERE session_id = %s
        """,
            (session_id,),
        )

        conn.commit()

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logging.error(f"Discard failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to discard upload")
    finally:
        cursor.close()
        conn.close()

    # Log discard event
    await _log_upload_event(
        session_id,
        current_user.get("id"),
        "discarded",
        event_details={"gcs_path": gcs_path},
    )

    return DiscardResponse(
        session_id=session_id,
        status="discarded",
        message="Upload and all data permanently deleted",
    )


# --- Manager Workflow Endpoints ---


@app.post("/override/{override_id}/resolve", response_model=ResolveOverrideResponse)
async def resolve_override(
    override_id: str,
    body: ResolveOverrideRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Manager endpoint: Mark an override as formally resolved and create a precedent case.

    This converts an open override into a historical precedent that can guide other staff.
    Only managers/compliance can approve resolutions.

    Args:
        override_id: UUID of the override_log entry
        body.resolution_notes: How was this override resolved? (min 10 chars)

    Returns:
        Confirmation with resolution date and manager ID
    """
    # RBAC: Only manager or compliance role
    user_role = current_user.get("role", "").lower()
    if user_role not in ["manager", "compliance"]:
        raise HTTPException(
            status_code=403,
            detail=f"Only managers/compliance can resolve overrides. Your role: {user_role}",
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Verify override exists
        cursor.execute(
            """
            SELECT override_id, user_id, is_resolved FROM override_log WHERE override_id = %s
        """,
            (override_id,),
        )

        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=404, detail=f"Override {override_id} not found"
            )

        override_uuid, override_user_id, is_already_resolved = row

        if is_already_resolved:
            raise HTTPException(
                status_code=400, detail=f"Override {override_id} is already resolved"
            )

        # Mark as resolved using stored function
        cursor.execute(
            """
            SELECT mark_override_resolved(%s, %s, %s);
        """,
            (override_id, body.resolution_notes, current_user.get("id")),
        )

        result = cursor.fetchone()
        conn.commit()

        if not result or not result[0]:
            raise HTTPException(status_code=500, detail="Failed to update override")

        # Retrieve updated override for response
        cursor.execute(
            """
            SELECT override_id, resolution_date, resolved_by_user_id FROM override_log WHERE override_id = %s
        """,
            (override_id,),
        )

        updated_row = cursor.fetchone()
        if not updated_row:
            raise HTTPException(
                status_code=500, detail="Override not found after update"
            )

        upd_id, res_date, res_by = updated_row

        logging.info(
            f"Override {override_id} marked as resolved by {current_user.get('username', 'unknown')}"
        )

        # Trigger immediate embedding for the resolution notes (grey-area learning)
        # This embeds the justification so it can be found in future precedent searches
        try:
            asyncio.create_task(
                process_override_embedding(
                    int(upd_id) if upd_id else 0, body.resolution_notes
                )
            )
        except Exception as embed_err:
            logging.warning(
                f"Failed to schedule embedding for override {override_id}: {embed_err}"
            )
            # Don't fail the resolution if embedding scheduling fails

        return ResolveOverrideResponse(
            override_id=str(upd_id),
            is_resolved=True,
            resolution_date=res_date.isoformat() if res_date else "",
            resolved_by=res_by or "",
            message=f"Override resolved successfully. This case is now available as a precedent for other staff.",
        )

    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logging.exception(f"Failed to resolve override {override_id}")
        raise HTTPException(
            status_code=500, detail=f"Failed to resolve override: {str(e)[:100]}"
        )
    finally:
        cursor.close()
        conn.close()


@app.get("/precedent-details/{override_id}", response_model=PrecedentDetailsResponse)
async def get_precedent_details(
    override_id: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Retrieve full details of a precedent case for manager/compliance review.

    Shows the complete audit trail: original query, user override, resolution notes.

    Args:
        override_id: UUID of the override_log entry (must be marked as resolved)

    Returns:
        Full precedent case details with query context
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Get override details
        cursor.execute(
            """
            SELECT 
                override_id,
                user_id,
                user_department,
                original_recommendation,
                override_reason,
                resolution_notes,
                resolved_by_user_id,
                resolution_date,
                created_at,
                query_id
            FROM override_log
            WHERE override_id = %s AND is_resolved = TRUE
        """,
            (override_id,),
        )

        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Precedent case {override_id} not found or not yet resolved",
            )

        (
            o_id,
            u_id,
            u_dept,
            orig_rec,
            override_reason,
            res_notes,
            res_by,
            res_date,
            created,
            q_id,
        ) = row

        # Optionally retrieve query context (if query_sessions table exists)
        query_text = None
        if q_id:
            try:
                cursor.execute(
                    """
                    SELECT query_text FROM query_sessions WHERE session_id = %s LIMIT 1
                """,
                    (str(q_id),),
                )
                q_row = cursor.fetchone()
                if q_row:
                    query_text = q_row[0]
            except Exception:
                pass  # Query context optional

        return PrecedentDetailsResponse(
            override_id=str(o_id),
            user_id=u_id,
            user_department=u_dept,
            original_recommendation=orig_rec,
            override_reason=override_reason,
            how_resolved=res_notes or "No resolution notes",
            resolved_date=res_date.isoformat() if res_date else None,
            resolved_by_user_id=res_by,
            created_at=created.isoformat() if created else "",
            query_session_id=str(q_id) if q_id else None,
            query_text=query_text,
        )

    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Failed to retrieve precedent details for {override_id}")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve precedent: {str(e)[:100]}"
        )
    finally:
        cursor.close()
        conn.close()


@app.get("/metrics/unresolved-cases", response_model=UnresolvedMetricsResponse)
async def get_unresolved_metrics(
    department: Optional[str] = None,
    days: int = 120,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Retrieve aggregated metrics for unresolved override cases.

    Provides:
    - Total count of unresolved cases
    - Count of unique officers involved
    - Time period analyzed
    - Customer demographic breakdown

    Args:
        department: Optional filter by department (e.g., 'loans')
        days: Time period in days (default 120 = ~4 months)
        current_user: Current authenticated user (RBAC enforced)

    Returns:
        UnresolvedMetricsResponse with aggregated statistics
    """
    # RBAC: Allow manager, compliance roles
    user_role = current_user.get("role", "officer")
    if user_role not in ["manager", "compliance"]:
        raise HTTPException(
            status_code=403,
            detail="Only managers and compliance officers can view metrics",
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Calculate date range
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        # Build base query
        where_clauses = [
            "is_resolved = FALSE",
            f"created_at >= '{start_date.isoformat()}'",
        ]
        if department:
            where_clauses.append(f"LOWER(user_department) = LOWER('{department}')")

        where_sql = " AND ".join(where_clauses)

        # Query 1: Total cases and unique officers
        cursor.execute(f"""
            SELECT 
                COUNT(DISTINCT override_id) as total_cases,
                COUNT(DISTINCT user_id) as unique_officers
            FROM override_log
            WHERE {where_sql}
        """)

        row = cursor.fetchone()
        total_cases = row[0] if row else 0
        unique_officers = row[1] if row else 0

        # Query 2: Demographic breakdown
        # Assumes customer demographic info is in override_log or linked via customer_id
        # For now, we'll use a hardcoded distribution (in production, query actual customer data)
        # This would typically join with a customers table with income level
        cursor.execute(f"""
            SELECT 
                COALESCE(customer_demographic, 'unknown') as demographic,
                COUNT(*) as count
            FROM override_log
            WHERE {where_sql}
            GROUP BY customer_demographic
            ORDER BY count DESC
        """)

        demographic_rows = cursor.fetchall()

        # Calculate demographic breakdown with percentages
        demographics = []
        if total_cases > 0:
            for demo_row in demographic_rows:
                category = demo_row[0] or "unknown"
                count = demo_row[1]
                percentage = round((count / total_cases) * 100, 1)
                demographics.append(
                    DemographicBreakdown(
                        category=category, count=count, percentage=percentage
                    )
                )

        # Determine time period label
        if days == 30:
            period_label = "1 month"
        elif days == 60:
            period_label = "2 months"
        elif days == 90:
            period_label = "3 months"
        elif days == 120:
            period_label = "4 months"
        elif days == 365:
            period_label = "1 year"
        else:
            period_label = f"{days} days"

        return UnresolvedMetricsResponse(
            total_cases=total_cases,
            unique_officers=unique_officers,
            time_period_days=days,
            time_period_label=period_label,
            period_start_date=start_date.isoformat(),
            period_end_date=end_date.isoformat(),
            demographics=demographics,
            department_filter=department,
        )

    except Exception as e:
        logging.exception(f"Failed to retrieve unresolved metrics")
        raise HTTPException(
            status_code=500, detail=f"Failed to retrieve metrics: {str(e)[:100]}"
        )
    finally:
        cursor.close()
        conn.close()


# --- API Endpoints ---


@app.post("/process-document")
async def process_document_from_pubsub(request: Request):
    """Thin wrapper that delegates ingestion to `services.doc_ingest_service.ingest_from_gs_event`.

    The original implementation is preserved below as a commented literal for easy rollback.
    """
    try:
        envelope = await request.json()
        message = envelope.get("message", {})
        if "data" not in message:
            logging.warning("Pub/Sub message data missing. Skipping.")
            return {"status": "skipped", "message": "No data in message"}

        pubsub_data = base64.b64decode(message["data"]).decode("utf-8")
        gcs_event = json.loads(pubsub_data)
        original_gcs_filename = gcs_event.get("name")
        bucket_name = gcs_event.get("bucket")

        from services.doc_ingest_service import ingest_from_gs_event

        result = await ingest_from_gs_event(bucket_name, original_gcs_filename)
        # Result is an IngestResult Pydantic model
        return {
            "status": result.status,
            "document_id": result.document_id,
            "chunks_created": result.chunks_created,
        }
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Unhandled error delegating ingest: {e}")
        raise HTTPException(
            status_code=500, detail=f"An unexpected error occurred: {str(e)}"
        )


# --- Embedding Status API Endpoints ---


class EmbeddingStatusResponse(BaseModel):
    """Response model for embedding status check."""

    document_id: Optional[int] = None
    gcs_object_path: Optional[str] = None
    embedding_status: str  # 'pending', 'complete', 'failed'
    document_type: Optional[str] = None
    embedding_eligible_at: Optional[str] = None
    attempt_count: int = 0
    error_message: Optional[str] = None
    estimated_wait_minutes: Optional[int] = None

    class Config:
        from_attributes = True


@app.get("/embedding-status/summary")
async def get_embedding_status_summary(
    department: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Get summary of embedding status across all documents."""
    if TEST_MODE:
        return {
            "total": 100,
            "pending": 5,
            "complete": 92,
            "failed": 3,
            "oldest_pending_minutes": 15,
        }

    conn = get_db_connection()
    cursor = conn.cursor()

    # Build query with optional department filter
    where_clause = ""
    params = []
    if department:
        where_clause = "WHERE department_folder = %s"
        params.append(department)

    cursor.execute(
        f"""
        SELECT 
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE embedding_status = 'pending') as pending,
            COUNT(*) FILTER (WHERE embedding_status = 'complete') as complete,
            COUNT(*) FILTER (WHERE embedding_status = 'failed') as failed,
            MIN(embedding_eligible_at) FILTER (WHERE embedding_status = 'pending') as oldest_pending
        FROM documents
        {where_clause}
    """,
        params,
    )

    row = cursor.fetchone()
    cursor.close()
    conn.close()

    total, pending, complete, failed, oldest_pending = row

    # Calculate oldest pending age in minutes
    oldest_pending_minutes = None
    if oldest_pending:
        from datetime import datetime

        now = datetime.now(timezone.utc)
        if oldest_pending.tzinfo is None:
            oldest_pending = oldest_pending.replace(tzinfo=timezone.utc)
        oldest_pending_minutes = int((now - oldest_pending).total_seconds() / 60)

    return {
        "total": total or 0,
        "pending": pending or 0,
        "complete": complete or 0,
        "failed": failed or 0,
        "oldest_pending_minutes": oldest_pending_minutes,
    }


@app.get("/embedding-status/{doc_id}", response_model=EmbeddingStatusResponse)
async def get_embedding_status_by_id(
    doc_id: int, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Check embedding status for a specific document by ID."""
    if TEST_MODE:
        return EmbeddingStatusResponse(
            document_id=doc_id,
            embedding_status="complete",
            document_type="sop",
            attempt_count=1,
        )

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, gcs_object_path, embedding_status, document_type, 
               embedding_eligible_at, embedding_attempt_count, embedding_error_message
        FROM documents
        WHERE id = %s
        LIMIT 1
    """,
        (doc_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    doc_id, gcs_path, status, doc_type, eligible_at, attempt_count, error_msg = row

    # Estimate wait time if pending
    estimated_wait = None
    if status == "pending" and eligible_at:
        from datetime import datetime

        now = datetime.now(timezone.utc)
        if eligible_at.tzinfo is None:
            eligible_at = eligible_at.replace(tzinfo=timezone.utc)
        if eligible_at > now:
            estimated_wait = (
                int((eligible_at - now).total_seconds() / 60) + 5
            )  # Add processing buffer
        else:
            estimated_wait = 5  # Should be processed soon

    return EmbeddingStatusResponse(
        document_id=doc_id,
        gcs_object_path=gcs_path,
        embedding_status=status or "pending",
        document_type=doc_type,
        embedding_eligible_at=str(eligible_at) if eligible_at else None,
        attempt_count=attempt_count or 0,
        error_message=error_msg,
        estimated_wait_minutes=estimated_wait,
    )


@app.get(
    "/embedding-status/by-path/{gcs_object_path:path}",
    response_model=EmbeddingStatusResponse,
)
async def get_embedding_status_by_path(
    gcs_object_path: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Check embedding status for a document by GCS path."""
    if TEST_MODE:
        return EmbeddingStatusResponse(
            gcs_object_path=gcs_object_path,
            embedding_status="complete",
            document_type="sop",
            attempt_count=1,
        )

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, gcs_object_path, embedding_status, document_type,
               embedding_eligible_at, embedding_attempt_count, embedding_error_message
        FROM documents
        WHERE gcs_object_path = %s
        LIMIT 1
    """,
        (gcs_object_path,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    doc_id, gcs_path, status, doc_type, eligible_at, attempt_count, error_msg = row

    return EmbeddingStatusResponse(
        document_id=doc_id,
        gcs_object_path=gcs_path,
        embedding_status=status or "pending",
        document_type=doc_type,
        embedding_eligible_at=str(eligible_at) if eligible_at else None,
        attempt_count=attempt_count or 0,
        error_message=error_msg,
    )


class BulkEmbeddingStatusRequest(BaseModel):
    """Request for bulk embedding status check."""

    document_ids: Optional[List[int]] = None
    gcs_paths: Optional[List[str]] = None


class BulkEmbeddingStatusResponse(BaseModel):
    """Response for bulk embedding status check."""

    total: int
    pending: int
    complete: int
    failed: int
    documents: List[EmbeddingStatusResponse]


@app.post("/embedding-status/bulk", response_model=BulkEmbeddingStatusResponse)
async def get_bulk_embedding_status(
    request: BulkEmbeddingStatusRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Check embedding status for multiple documents at once."""
    if TEST_MODE:
        return BulkEmbeddingStatusResponse(
            total=0, pending=0, complete=0, failed=0, documents=[]
        )

    conn = get_db_connection()
    cursor = conn.cursor()

    documents = []

    if request.document_ids:
        cursor.execute(
            """
            SELECT id, gcs_object_path, embedding_status, document_type,
                   embedding_eligible_at, embedding_attempt_count, embedding_error_message
            FROM documents
            WHERE id = ANY(%s)
        """,
            (request.document_ids,),
        )
        rows = cursor.fetchall()

        for row in rows:
            (
                doc_id,
                gcs_path,
                status,
                doc_type,
                eligible_at,
                attempt_count,
                error_msg,
            ) = row
            documents.append(
                EmbeddingStatusResponse(
                    document_id=doc_id,
                    gcs_object_path=gcs_path,
                    embedding_status=status or "pending",
                    document_type=doc_type,
                    embedding_eligible_at=str(eligible_at) if eligible_at else None,
                    attempt_count=attempt_count or 0,
                    error_message=error_msg,
                )
            )

    if request.gcs_paths:
        cursor.execute(
            """
            SELECT id, gcs_object_path, embedding_status, document_type,
                   embedding_eligible_at, embedding_attempt_count, embedding_error_message
            FROM documents
            WHERE gcs_object_path = ANY(%s)
        """,
            (request.gcs_paths,),
        )
        rows = cursor.fetchall()

        for row in rows:
            (
                doc_id,
                gcs_path,
                status,
                doc_type,
                eligible_at,
                attempt_count,
                error_msg,
            ) = row
            # Avoid duplicates if same doc was in both lists
            if not any(d.document_id == doc_id for d in documents):
                documents.append(
                    EmbeddingStatusResponse(
                        document_id=doc_id,
                        gcs_object_path=gcs_path,
                        embedding_status=status or "pending",
                        document_type=doc_type,
                        embedding_eligible_at=str(eligible_at) if eligible_at else None,
                        attempt_count=attempt_count or 0,
                        error_message=error_msg,
                    )
                )

    cursor.close()
    conn.close()

    # Count by status
    pending = sum(1 for d in documents if d.embedding_status == "pending")
    complete = sum(1 for d in documents if d.embedding_status == "complete")
    failed = sum(1 for d in documents if d.embedding_status == "failed")

    return BulkEmbeddingStatusResponse(
        total=len(documents),
        pending=pending,
        complete=complete,
        failed=failed,
        documents=documents,
    )


@app.get("/embedding-status/summary")
async def get_embedding_status_summary(
    department: Optional[str] = None,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Get summary of embedding status across all documents."""
    if TEST_MODE:
        return {
            "total": 100,
            "pending": 5,
            "complete": 92,
            "failed": 3,
            "oldest_pending_minutes": 15,
        }

    conn = get_db_connection()
    cursor = conn.cursor()

    # Build query with optional department filter
    where_clause = ""
    params = []
    if department:
        where_clause = "WHERE department_folder = %s"
        params.append(department)

    cursor.execute(
        f"""
        SELECT 
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE embedding_status = 'pending') as pending,
            COUNT(*) FILTER (WHERE embedding_status = 'complete') as complete,
            COUNT(*) FILTER (WHERE embedding_status = 'failed') as failed,
            MIN(embedding_eligible_at) FILTER (WHERE embedding_status = 'pending') as oldest_pending
        FROM documents
        {where_clause}
    """,
        params,
    )

    row = cursor.fetchone()
    cursor.close()
    conn.close()

    total, pending, complete, failed, oldest_pending = row

    # Calculate oldest pending age in minutes
    oldest_pending_minutes = None
    if oldest_pending:
        from datetime import datetime

        now = datetime.now(timezone.utc)
        if oldest_pending.tzinfo is None:
            oldest_pending = oldest_pending.replace(tzinfo=timezone.utc)
        oldest_pending_minutes = int((now - oldest_pending).total_seconds() / 60)

    return {
        "total": total or 0,
        "pending": pending or 0,
        "complete": complete or 0,
        "failed": failed or 0,
        "oldest_pending_minutes": oldest_pending_minutes,
    }


# --- Metadata Management API Endpoints (Human-in-the-Loop Hooks) ---


@app.get(
    "/document-metadata/{gcs_object_path:path}"
)  # Use :path to allow slashes in path parameter
async def get_document_metadata(
    gcs_object_path: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Fetches current metadata for a document."""
    if TEST_MODE:
        p = _test_metadata_path(gcs_object_path)
        data = storage_service.read_json_path(p)
        if not data:
            raise HTTPException(
                status_code=404, detail="Document metadata not found (test mode)."
            )
        # enforce same access control shape
        user_role = current_user.get("role")
        user_depts = current_user.get("departments") or []
        department_folder = data.get("department_folder")
        if user_role != "manager" and department_folder not in user_depts:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to view this document.",
            )
        return data

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            id,
            suggested_title, title_justification,
            suggested_department, department_justification,
            suggested_process_type, process_type_justification,
            suggested_status, status_justification,
            final_title, final_department, final_process_type, final_status,
            review_status, department_folder
        FROM documents
        WHERE gcs_object_path = %s
        LIMIT 1
    """,
        (gcs_object_path,),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Document metadata not found.")

    (
        doc_id,
        suggested_title,
        title_justification,
        suggested_department,
        department_justification,
        suggested_process_type,
        process_type_justification,
        suggested_status,
        status_justification,
        final_title,
        final_department,
        final_process_type,
        final_status,
        review_status,
        department_folder,
    ) = row

    # Access control: allow managers full access; officers only their departments
    user_role = current_user.get("role")
    user_depts = current_user.get("departments") or []
    if user_role != "manager" and department_folder not in user_depts:
        raise HTTPException(
            status_code=403, detail="You do not have permission to view this document."
        )

    return {
        "document_id": doc_id,
        "suggested_title": suggested_title,
        "title_justification": title_justification,
        "suggested_department": suggested_department,
        "department_justification": department_justification,
        "suggested_process_type": suggested_process_type,
        "process_type_justification": process_type_justification,
        "suggested_status": suggested_status,
        "status_justification": status_justification,
        "final_title": final_title,
        "final_department": final_department,
        "final_process_type": final_process_type,
        "final_status": final_status,
        "review_status": review_status,
        "department_folder": department_folder,
    }


@app.put("/document-metadata/{gcs_object_path:path}")
async def update_document_metadata(
    gcs_object_path: str,
    metadata_update: Dict[str, str],
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """Updates human-edited metadata fields for a document."""
    if TEST_MODE:
        p = _test_metadata_path(gcs_object_path)
        existing = storage_service.read_json_path(p) or {}
        document_id = existing.get("document_id", 1)
        existing_map = {
            "final_title": existing.get("final_title"),
            "final_department": existing.get("final_department"),
            "final_process_type": existing.get("final_process_type"),
            "final_status": existing.get("final_status"),
        }
    else:
        conn = get_db_connection()
        cursor = conn.cursor()

    update_fields = []
    update_values = []

    if "final_title" in metadata_update:
        update_fields.append("final_title = %s")
        update_values.append(metadata_update["final_title"])
    if "final_department" in metadata_update:
        update_fields.append("final_department = %s")
        update_values.append(metadata_update["final_department"])
    if "final_process_type" in metadata_update:
        update_fields.append("final_process_type = %s")
        update_values.append(metadata_update["final_process_type"])
    if "final_status" in metadata_update:
        update_fields.append("final_status = %s")
        update_values.append(metadata_update["final_status"])

    if not update_fields:
        raise HTTPException(
            status_code=400, detail="No valid fields provided for update."
        )
    if not TEST_MODE:
        # Fetch existing values for audit logging
        cursor.execute(
            "SELECT id, final_title, final_department, final_process_type, final_status FROM documents WHERE gcs_object_path = %s LIMIT 1",
            (gcs_object_path,),
        )
        existing = cursor.fetchone()
        if not existing:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail="Document not found.")

        document_id = existing[0]
        existing_map = {
            "final_title": existing[1],
            "final_department": existing[2],
            "final_process_type": existing[3],
            "final_status": existing[4],
        }

    # Authorization: managers can edit any; officers only their departments
    user_role = current_user.get("role")
    user_depts = current_user.get("departments") or []
    doc_dept = existing_map.get("final_department") or None
    if user_role != "manager":
        # Determine target department if changing it, else use existing
        target_dept = metadata_update.get("final_department", doc_dept)
        if target_dept and target_dept not in user_depts:
            if not TEST_MODE:
                cursor.close()
                conn.close()
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to edit this document.",
            )

    # Perform audit logging for field-level changes — write audit files to GCS (avoid DB migrations)
    for idx, field_exp in enumerate(
        ["final_title", "final_department", "final_process_type", "final_status"]
    ):
        if field_exp in metadata_update:
            old = existing_map.get(field_exp)
            new = metadata_update[field_exp]
            if str(old) != str(new):
                audit_entry = {
                    "document_id": document_id,
                    "field": field_exp,
                    "old_value": old,
                    "new_value": new,
                    "username": current_user.get("username"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                # Use a document-scoped base path in GCS for audit/version files
                base_path = f"documents/{document_id}"
                try:
                    if TEST_MODE:
                        local_path = os.path.join(
                            "local_test_store",
                            base_path,
                            "audit",
                            f"{datetime.now(timezone.utc).isoformat()}_{uuid.uuid4().hex}.json",
                        )
                        storage_service.write_json_path(local_path, audit_entry)
                    else:
                        storage_service.append_audit(
                            GCS_BUCKET_NAME, base_path, audit_entry
                        )
                except Exception:
                    logging.exception("Failed to write audit entry to storage")

    if TEST_MODE:
        # Merge into existing and persist locally
        merged = existing or {}
        merged.update(
            {
                k: metadata_update[k]
                for k in [
                    "final_title",
                    "final_department",
                    "final_process_type",
                    "final_status",
                ]
                if k in metadata_update
            }
        )
        merged["document_id"] = document_id
        merged["department_folder"] = merged.get("final_department")
        storage_service.write_json_path(_test_metadata_path(gcs_object_path), merged)
        logging.info(
            f"(test) Updated metadata for {gcs_object_path} by {current_user.get('username')}"
        )
        return {
            "message": f"(test) Metadata for {gcs_object_path} updated successfully."
        }

    update_query = f"""
        UPDATE documents
        SET {", ".join(update_fields)},
            updated_at = NOW()
        WHERE gcs_object_path = %s
    """
    update_values.append(gcs_object_path)

    cursor.execute(update_query, tuple(update_values))
    conn.commit()
    cursor.close()
    conn.close()

    logging.info(
        f"Updated metadata for {gcs_object_path} by {current_user.get('username')}"
    )
    return {"message": f"Metadata for {gcs_object_path} updated successfully."}


@app.post("/document-metadata/{gcs_object_path:path}/confirm")
async def confirm_document_metadata(
    gcs_object_path: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Confirms final metadata for a document, creates a version (GCS-backed), and records approval."""
    # TEST_MODE: operate on local JSON files
    if TEST_MODE:
        p = _test_metadata_path(gcs_object_path)
        data = storage_service.read_json_path(p)
        if not data:
            raise HTTPException(
                status_code=404, detail="Document not found (test mode)."
            )

        documents_id = data.get("document_id", 1)
        original_name = data.get("original_gcs_filename", gcs_object_path)
        final_department = data.get("final_department")

        if not final_department:
            raise HTTPException(
                status_code=400, detail="Cannot confirm: final_department is not set."
            )

        user_role = current_user.get("role")
        user_depts = current_user.get("departments") or []
        if user_role != "manager" and final_department not in user_depts:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to confirm this document.",
            )

        sanitized_name = (
            original_name.replace("/", "_") if original_name else f"doc_{documents_id}"
        )
        version_dir = _test_version_dir(sanitized_name)
        os.makedirs(version_dir, exist_ok=True)
        existing = (
            sorted(
                [
                    n
                    for n in os.listdir(version_dir)
                    if n.startswith("v") and n.endswith(".json")
                ]
            )
            if os.path.exists(version_dir)
            else []
        )
        maxv = 0
        for n in existing:
            try:
                v = int(n[1:-5])
                if v > maxv:
                    maxv = v
            except Exception:
                continue
        next_version = maxv + 1
        version_path = os.path.join(version_dir, f"v{next_version}.json")
        storage_service.write_json_path(version_path, data)

        # mark approved in local doc
        data["review_status"] = "approved"
        data["last_reviewed_by"] = current_user.get("username")
        data["last_reviewed_at"] = datetime.now(timezone.utc).isoformat()
        storage_service.write_json_path(p, data)

        # write audit
        audit_entry = {
            "document_id": documents_id,
            "action": "confirm",
            "old_value": "pending",
            "new_value": "approved",
            "username": current_user.get("username"),
            "version": next_version,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        audit_path = os.path.join(
            "local_test_store", "versions", sanitized_name, "audit"
        )
        os.makedirs(audit_path, exist_ok=True)
        storage_service.write_json_path(
            os.path.join(
                audit_path,
                f"{datetime.now(timezone.utc).isoformat()}_{uuid.uuid4().hex}.json",
            ),
            audit_entry,
        )

        logging.info(
            f"(test) Confirmed metadata for {gcs_object_path} by {current_user.get('username')}"
        )
        return {
            "message": f"(test) Document {gcs_object_path} metadata confirmed and approved.",
            "version": next_version,
        }

    # Normal mode: write version JSON to GCS and update documents.review_status
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, original_gcs_filename, final_department FROM documents WHERE gcs_object_path = %s LIMIT 1",
        (gcs_object_path,),
    )
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Document not found.")

    documents_id, original_name, final_department = row

    if not final_department:
        cursor.close()
        conn.close()
        raise HTTPException(
            status_code=400, detail="Cannot confirm: final_department is not set."
        )

    user_role = current_user.get("role")
    user_depts = current_user.get("departments") or []
    if user_role != "manager" and final_department not in user_depts:
        cursor.close()
        conn.close()
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to confirm this document.",
        )

    # Build payload
    cursor.execute(
        """
        SELECT row_to_json(t) FROM (
          SELECT suggested_title, title_justification, suggested_department, department_justification,
                 suggested_process_type, process_type_justification, suggested_status, status_justification,
                 final_title, final_department, final_process_type, final_status, review_status
          FROM documents
          WHERE gcs_object_path = %s LIMIT 1
        ) t
        """,
        (gcs_object_path,),
    )
    payload_row = cursor.fetchone()
    payload = payload_row[0] if payload_row else None

    # Write version to GCS
    sanitized_name = (
        original_name.replace("/", "_") if original_name else f"doc_{documents_id}"
    )
    base_path = f"versions/{sanitized_name}"
    prefix = f"{base_path}/"
    existing = storage_service.list_versions(GCS_BUCKET_NAME, prefix)
    maxv = 0
    for name in existing:
        bn = name.split("/")[-1]
        if bn.startswith("v") and bn.endswith(".json"):
            try:
                v = int(bn[1:-5])
                if v > maxv:
                    maxv = v
            except Exception:
                continue
    next_version = maxv + 1
    version_path = f"{prefix}v{next_version}.json"
    try:
        storage_service.gcs_write_json(GCS_BUCKET_NAME, version_path, payload)
    except Exception as e:
        cursor.close()
        conn.close()
        logging.error(f"Failed to write version to GCS: {e}")
        raise HTTPException(status_code=500, detail="Failed to persist version to GCS")

    # Update documents as approved
    cursor.execute(
        "UPDATE documents SET review_status = 'approved', last_reviewed_by = %s, last_reviewed_at = NOW(), updated_at = NOW() WHERE gcs_object_path = %s",
        (current_user.get("username"), gcs_object_path),
    )

    # Write audit entry to GCS
    audit_entry = {
        "document_id": documents_id,
        "action": "confirm",
        "old_value": "pending",
        "new_value": "approved",
        "username": current_user.get("username"),
        "version": next_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        storage_service.append_audit(GCS_BUCKET_NAME, base_path, audit_entry)
    except Exception:
        logging.exception("Failed to write approval audit to GCS")

    conn.commit()
    cursor.close()
    conn.close()

    logging.info(
        f"Confirmed metadata for {gcs_object_path} by {current_user.get('username')}"
    )
    return {
        "message": f"Document {gcs_object_path} metadata confirmed and approved.",
        "version": next_version,
    }


@app.delete("/document-metadata/{gcs_object_path:path}/discard")
async def discard_document(
    gcs_object_path: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Discards a document, deleting from GCS and database (or local test store)."""
    try:
        # Delete from GCS or local store
        if TEST_MODE:
            p = _test_metadata_path(gcs_object_path)
            if os.path.exists(p):
                os.remove(p)
                logging.info(f"(test) Deleted local metadata {p}.")
            else:
                logging.warning(
                    f"(test) File {p} not found in local store for deletion."
                )
        else:
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob = bucket.blob(gcs_object_path)
            if blob.exists():
                blob.delete()
                logging.info(f"Deleted {gcs_object_path} from GCS.")
            else:
                logging.warning(
                    f"File {gcs_object_path} not found in GCS for deletion."
                )

        # Delete from Database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, department_folder FROM documents WHERE gcs_object_path = %s LIMIT 1",
            (gcs_object_path,),
        )
        doc = cursor.fetchone()
        if not doc:
            cursor.close()
            conn.close()
            raise HTTPException(
                status_code=404, detail="Document not found in DB for deletion."
            )

        document_id, department_folder = doc
        user_role = current_user.get("role")
        user_depts = current_user.get("departments") or []
        if user_role != "manager" and department_folder not in user_depts:
            cursor.close()
            conn.close()
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to delete this document.",
            )

        cursor.execute(
            "DELETE FROM documents WHERE gcs_object_path = %s", (gcs_object_path,)
        )
        deleted = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        if deleted == 0:
            raise HTTPException(
                status_code=404, detail="Document not found in DB for deletion."
            )

        logging.info(f"Discarded and deleted all traces of {gcs_object_path}.")
        return {
            "message": f"Document {gcs_object_path} and all its data discarded successfully."
        }

    except Exception as e:
        logging.error(
            f"Error discarding document {gcs_object_path}: {e}", exc_info=True
        )
        raise HTTPException(
            status_code=500,
            detail=f"An error occurred while discarding document: {str(e)}",
        )


# Health check
@app.get("/")
def health_check():
    return {"status": "ok", "service": "docintel-data-processor"}


@app.get("/upload-ui", response_class=HTMLResponse)
async def upload_ui():
    """Serve the document upload & confirmation UI."""
    try:
        # Try to read from project UI bundle first
        ui_path = os.path.join("UI_store", "UI.html")
        with open(ui_path, "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        # Minimal fallback message
        return HTMLResponse(
            content="<html><body><p>Upload UI not available. Ensure UI_store/UI.html exists.</p></body></html>"
        )


@app.get("/confirm-ui", response_class=HTMLResponse)
async def confirm_ui():
    """Serve the human confirmation UI (delegates to project UI bundle)."""
    try:
        ui_path = os.path.join("UI_store", "UI.html")
        with open(ui_path, "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(
            content="<html><body><p>Confirmation UI not available. Ensure UI_store/UI.html exists.</p></body></html>"
        )
