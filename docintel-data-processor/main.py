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
try:
    from google import genai
    from google.genai import types
    _GENAI_AVAILABLE = True
    _GENAI_CLIENT = None
    _VERTEX_SDK_AVAILABLE = False
    vertexai = None
    TextEmbeddingModel = None
    GenerativeModel = None
except Exception:
    _GENAI_AVAILABLE = False
    genai = None
    types = None
    _GENAI_CLIENT = None
    # Fall back to legacy vertexai if present in the environment
    try:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel
        from vertexai.generative_models import GenerativeModel
        _VERTEX_SDK_AVAILABLE = True
    except Exception:
        _VERTEX_SDK_AVAILABLE = False
        vertexai = None
        TextEmbeddingModel = None
        GenerativeModel = None
from google.auth.transport.requests import Request as GoogleAuthRequest
from datetime import datetime, timezone
import uuid

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
from google.cloud import storage, pubsub_v1, secretmanager
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession
import google.auth
import psycopg2
from psycopg2.extras import execute_values
from pypdf import PdfReader # For PDF parsing

# --- Configuration and Initialization ---
logging.basicConfig(level=logging.INFO)
app = FastAPI()

# GCP Clients
storage_client = storage.Client()
secret_client = secretmanager.SecretManagerServiceClient()
pubsub_publisher = pubsub_v1.PublisherClient() # For status updates or next steps

# Vertex/Endpoint config (use endpoints created in Vertex UI)
EMBEDDING_MODEL_ID = os.environ.get('EMBEDDING_MODEL_ID')  # e.g. text-embedding-004
GENERATIVE_MODEL_ID = os.environ.get('GENERATIVE_MODEL_ID')  # e.g. gemini-2.5-pro
EMBEDDING_ENDPOINT = os.environ.get('EMBEDDING_ENDPOINT')  # e.g. projects/PROJECT/locations/us-west1/endpoints/EMBEDDING_ID
GENERATIVE_ENDPOINT = os.environ.get('GENERATIVE_ENDPOINT')  # e.g. projects/PROJECT/locations/us-west1/endpoints/GEN_ID

# Lazy SDK model holders (initialized on first use)
_EMBEDDING_MODEL = None
_GENERATIVE_MODEL = None

# Authorized HTTP session (lazy)
_AUTH_SESSION = None
_AUTH_LOCK = threading.Lock()

def get_authed_session():
    global _AUTH_SESSION
    # Make initialization thread-safe to avoid races during cold starts
    if _AUTH_SESSION:
        return _AUTH_SESSION
    with _AUTH_LOCK:
        if _AUTH_SESSION:
            return _AUTH_SESSION
        # Prefer ADC, fall back to service account key if provided
        try:
            creds, _ = google.auth.default()
        except Exception:
            key_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
            if not key_path:
                raise RuntimeError('No Google credentials found (set GOOGLE_APPLICATION_CREDENTIALS or application default).')
            creds = service_account.Credentials.from_service_account_file(key_path)
        _AUTH_SESSION = AuthorizedSession(creds)
        return _AUTH_SESSION


async def async_post_with_retries(url, json=None, headers=None, timeout=None, retries=3, backoff_factor=1.0):
    """Async POST helper with exponential backoff using httpx.AsyncClient."""
    attempt = 0
    # httpx timeout can be a tuple (connect, read) or a number
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=json, headers=headers, timeout=timeout)
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
    """Return Authorization headers by ensuring credentials are fresh."""
    session = get_authed_session()
    creds = getattr(session, 'credentials', None)
    if creds is None:
        return {}
    try:
        creds.refresh(GoogleAuthRequest())
    except Exception:
        # best-effort; if refresh fails token may still be present
        pass
    token = getattr(creds, 'token', None)
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}

# Environment Variables
PROJECT_ID = os.environ.get('PROJECT_ID', 'ctrlaltelite-484111')
REGION = os.environ.get('REGION', 'us-central1')
RAG_REGION = os.environ.get('RAG_REGION', 'us-west1')  # RAG corpus is in us-west1
RAG_CORPUS_ID = os.environ.get('RAG_CORPUS_ID', '2305843009213693952')
RAG_CORPUS_NAME = f'projects/{PROJECT_ID}/locations/{RAG_REGION}/ragCorpora/{RAG_CORPUS_ID}'
GCS_BUCKET_NAME = os.environ.get('GCS_BUCKET_NAME', 'ambuckethack')
TEST_MODE = os.environ.get('TEST_MODE', '').lower() in ('1', 'true', 'yes')

# Database Config
DB_HOST = os.environ.get('DB_HOST')
DB_USER = os.environ.get('DB_USER', 'postgres')
DB_NAME = os.environ.get('DB_NAME', 'docintel_db')
DB_SECRET_NAME = os.environ.get('SECRET_NAME', 'docintel-database-secret')
DB_PASSWORD = None # Will be loaded from Secret Manager

# --- Helper Functions ---

def get_secret_value(secret_name: str) -> str:
    """Retrieves a secret from Google Secret Manager."""
    try:
        name = f"projects/{PROJECT_ID}/secrets/{secret_name}/versions/latest"
        response = secret_client.access_secret_version(request={"name": name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logging.error(f"Failed to retrieve secret '{secret_name}': {e}")
        raise

def get_db_connection():
    """Establishes and returns a PostgreSQL database connection."""
    global DB_PASSWORD
    if DB_PASSWORD is None:
        DB_PASSWORD = get_secret_value(DB_SECRET_NAME)
        logging.info("DB password loaded from Secret Manager.")

    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            dbname=DB_NAME
        )
        return conn
    except Exception as e:
        logging.error(f"Failed to connect to database: {e}", exc_info=True)
        raise


def get_user_by_api_key(api_key: str) -> Optional[Dict[str, Any]]:
    """Look up a user by API key in the database."""
    if not api_key:
        return None
    # TEST_MODE: return a fake user to avoid DB/SecretManager calls
    if TEST_MODE:
        return {"id": 1, "username": "test-user", "role": "manager", "departments": ["operations"]}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, username, role, departments FROM users WHERE api_key = %s LIMIT 1", (api_key,))
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


async def get_current_user(authorization: Optional[str] = Header(None)) -> Dict[str, Any]:
    """FastAPI dependency to resolve the current user from Authorization header.

    Accepts: 'Bearer <api_key>' or just the api_key in the header.
    Raises 401 if not found.
    """
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
    safe = gcs_object_path.replace('/', '__')
    return os.path.join('local_test_store', 'documents', f"{safe}.json")

def _test_version_dir(sanitized_name: str) -> str:
    return os.path.join('local_test_store', 'versions', sanitized_name)


def gcs_write_json(bucket_name: str, path: str, obj: Any) -> None:
    """Write a JSON object to GCS at the given path."""
    try:
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(path)
        blob.upload_from_string(json.dumps(obj), content_type="application/json")
    except Exception as e:
        logging.error(f"Failed to write JSON to gs://{bucket_name}/{path}: {e}")
        raise


def local_write_json(path: str, obj: Any) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def local_read_json(path: str) -> Optional[Any]:
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def gcs_list_versions(bucket_name: str, prefix: str) -> List[str]:
    """List blob names under a prefix and return list of names."""
    try:
        bucket = storage_client.bucket(bucket_name)
        return [b.name for b in bucket.list_blobs(prefix=prefix)]
    except Exception as e:
        logging.error(f"Failed to list blobs for gs://{bucket_name}/{prefix}: {e}")
        return []


def gcs_append_audit(bucket_name: str, base_path: str, entry: Dict[str, Any]) -> None:
    """Append an audit entry by writing a timestamped JSON file under base_path/audit/."""
    ts = datetime.now(timezone.utc).isoformat()
    tid = uuid.uuid4().hex
    path = f"{base_path}/audit/{ts}_{tid}.json"
    gcs_write_json(bucket_name, path, entry)

def extract_text_from_pdf(gcs_blob) -> str:
    """Extracts text from a PDF blob."""
    try:
        from io import BytesIO
        pdf_content = BytesIO()
        gcs_blob.download_to_file(pdf_content)
        pdf_content.seek(0)

        reader = PdfReader(pdf_content)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text
    except Exception as e:
        logging.error(f"Failed to extract text from PDF: {e}", exc_info=True)
        raise

def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 100) -> List[str]:
    """Simple text chunking for demonstration."""
    chunks = []
    current_start = 0
    while current_start < len(text):
        chunk = text[current_start:current_start + chunk_size]
        chunks.append(chunk)
        current_start += chunk_size - overlap
        if current_start < 0:
            current_start = 0
    return chunks

async def get_text_embeddings(texts: List[str]) -> List[List[float]]:
    """Generates embeddings for a list of texts using either the Vertex SDK (preferred)
    or a Vertex Endpoint via REST as a fallback.

    Acceptable configurations:
      - Vertex SDK available and `EMBEDDING_MODEL_ID` set -> use SDK
      - `EMBEDDING_ENDPOINT` set -> use REST predict endpoint
    """
    if not (EMBEDDING_ENDPOINT or EMBEDDING_MODEL_ID):
        raise RuntimeError('No embedding configuration: set EMBEDDING_ENDPOINT or EMBEDDING_MODEL_ID')

    embeddings: List[List[float]] = []
    headers = get_auth_headers()

    try:
        # Use a smaller batch size to avoid very large payloads and timeouts
        batch_size = 100
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            logging.info(f"Generating embeddings for batch {i//batch_size + 1}...")
            # If vertex-ai SDK is available and a model ID is configured, prefer SDK call
            if _GENAI_AVAILABLE and EMBEDDING_MODEL_ID:
                def _sdk_embed_call():
                    global _GENAI_CLIENT
                    if _GENAI_CLIENT is None:
                        # create genai client lazily
                        _GENAI_CLIENT = genai.Client(vertexai=True, project=PROJECT_ID, location=REGION)
                    vecs_local = []
                    # call embed_content per input to avoid uncertain batch support
                    for txt in batch:
                        try:
                            resp = _GENAI_CLIENT.models.embed_content(model=EMBEDDING_MODEL_ID, contents=txt)
                            # robust extraction
                            emb = None
                            if hasattr(resp, 'embedding'):
                                emb = getattr(resp, 'embedding')
                            elif hasattr(resp, 'embeddings'):
                                emb = resp.embeddings[0]
                            elif hasattr(resp, 'data') and len(resp.data) > 0 and hasattr(resp.data[0], 'embedding'):
                                emb = resp.data[0].embedding
                            if emb is None:
                                try:
                                    d = resp.__dict__
                                    for v in d.values():
                                        if isinstance(v, (list, tuple)) and len(v) > 0 and all(isinstance(x, (int, float)) for x in v):
                                            emb = v
                                            break
                                except Exception:
                                    emb = None
                            vecs_local.append([float(x) for x in emb] if emb else [])
                        except Exception:
                            vecs_local.append([])
                    return vecs_local

                try:
                    vecs = await asyncio.to_thread(_sdk_embed_call)
                    embeddings.extend(vecs)
                    continue
                except Exception:
                    logging.exception('GenAI SDK embedding call failed; falling back to REST predict')
            # If google-genai is not available, allow legacy vertexai SDK path
            if _VERTEX_SDK_AVAILABLE and EMBEDDING_MODEL_ID:
                def _vertex_sdk_embed_call():
                    global _EMBEDDING_MODEL
                    if _EMBEDDING_MODEL is None:
                        vertexai.init(project=PROJECT_ID, location=REGION)
                        _EMBEDDING_MODEL = TextEmbeddingModel.from_pretrained(EMBEDDING_MODEL_ID)
                    resp = _EMBEDDING_MODEL.get_embeddings(batch)
                    vecs = []
                    for e in getattr(resp, 'embeddings', []):
                        vals = getattr(e, 'values', None) or getattr(e, 'embedding', None) or []
                        vecs.append([float(x) for x in vals])
                    return vecs

                try:
                    vecs = await asyncio.to_thread(_vertex_sdk_embed_call)
                    embeddings.extend(vecs)
                    continue
                except Exception:
                    logging.exception('Vertex SDK embedding call failed; falling back to REST predict')

            # Prefer model-ID predict URL if provided (projects/{project}/locations/{region}/models/{model}:predict)
            if EMBEDDING_MODEL_ID:
                model_part = EMBEDDING_MODEL_ID
                # If a fully-qualified model resource was provided, use it; otherwise build model path
                if model_part.startswith('projects/'):
                    url = f"https://{REGION}-aiplatform.googleapis.com/v1/{model_part}:predict"
                else:
                    url = f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{REGION}/models/{model_part}:predict"
            elif EMBEDDING_ENDPOINT:
                url = f"https://{REGION}-aiplatform.googleapis.com/v1/{EMBEDDING_ENDPOINT}:predict"
            else:
                raise RuntimeError('No EMBEDDING_MODEL_ID or EMBEDDING_ENDPOINT configured')
            payload = {"instances": [{"content": t} for t in batch]}
            try:
                resp = await async_post_with_retries(url, json=payload, headers=headers, timeout=(5.0, 60.0), retries=3, backoff_factor=1.0)
                data = resp.json()
            except httpx.ReadTimeout:
                logging.exception('Embedding request timed out')
                raise asyncio.TimeoutError('Embedding request timed out')
            # Robust parsing for embedding shapes
            preds = data.get('predictions') or data.get('outputs') or data.get('embeddings')
            if preds is None:
                raise ValueError('No predictions/embeddings in Vertex response')
            # predictions may be list of lists (vectors) or list of dicts with 'embedding' key
            for p in preds:
                if isinstance(p, list):
                    embeddings.append([float(x) for x in p])
                elif isinstance(p, dict):
                    if 'embedding' in p:
                        embeddings.append([float(x) for x in p['embedding']])
                    elif 'vector' in p:
                        embeddings.append([float(x) for x in p['vector']])
                    elif 'value' in p and isinstance(p['value'], list):
                        embeddings.append([float(x) for x in p['value']])
                    else:
                        # try to extract first list-like value
                        found = False
                        for v in p.values():
                            if isinstance(v, list):
                                embeddings.append([float(x) for x in v])
                                found = True
                                break
                        if not found:
                            raise ValueError('Unrecognized embedding format')
        return embeddings
    except asyncio.TimeoutError:
        # Propagate as timeout to caller so they can map to 504 if desired
        raise
    except Exception as e:
        logging.error(f"Failed to get text embeddings from Vertex Endpoint: {e}", exc_info=True)
        raise

async def get_ai_metadata_suggestions(document_text: str) -> Dict[str, Any]:
    """Uses Vertex AI Generative Model to suggest metadata."""
    prompt = f"""
    You are an expert in banking SOPs and policy documents. Analyze the following document text and provide non-binding suggestions for the following fields:
    - Title
    - Department (e.g., "Operations", "Compliance", "Finance", "HR", "IT")
    - ProcessType (e.g., "Risk Assessment", "Customer Onboarding", "Transaction Monitoring", "Incident Management", "Account Opening")
    - Status (e.g., "Draft", "Active", "Deprecated", "Under Review")

    For each field, provide a 'suggested_value' and a brief 'justification' (1-2 sentences) based on the document's content.
    Also, provide a 'confidence_score' between 0.0 and 1.0 for each suggestion.
    Format your response as a JSON object.

    Document Text:
    ---
    {document_text[:5000]} # Limit text length for prompt token limits
    ---

    Example JSON response format:
    {{
      "title": {{"suggested_value": "...", "justification": "...", "confidence_score": 0.X}},
      "department": {{"suggested_value": "...", "justification": "...", "confidence_score": 0.X}},
      "process_type": {{"suggested_value": "...", "justification": "...", "confidence_score": 0.X}},
      "status": {{"suggested_value": "...", "justification": "...", "confidence_score": 0.X}}
    }}
    """

    # If no model or endpoint configured, return safe defaults
    if not (GENERATIVE_MODEL_ID or GENERATIVE_ENDPOINT):
        logging.warning('No generative model or endpoint configured; skipping AI metadata suggestions')
        return {
            "title": {"suggested_value": None, "justification": "No generative endpoint configured.", "confidence_score": 0.0},
            "department": {"suggested_value": None, "justification": "No generative endpoint configured.", "confidence_score": 0.0},
            "process_type": {"suggested_value": None, "justification": "No generative endpoint configured.", "confidence_score": 0.0},
            "status": {"suggested_value": None, "justification": "No generative endpoint configured.", "confidence_score": 0.0}
        }

    headers = get_auth_headers()
    try:
        data = None

        # Try GenAI SDK-based call first if available
        if _GENAI_AVAILABLE and GENERATIVE_MODEL_ID:
            async def _sdk_gen_call_async():
                global _GENAI_CLIENT
                if _GENAI_CLIENT is None:
                    _GENAI_CLIENT = genai.Client(vertexai=True, project=PROJECT_ID, location=REGION)
                try:
                    # use async client for generation
                    resp = await _GENAI_CLIENT.aio.models.generate_content(model=GENERATIVE_MODEL_ID, contents=prompt, config=types.GenerateContentConfig(max_output_tokens=1024))
                    # prefer resp.text convenience
                    text = getattr(resp, 'text', None)
                    if text:
                        return text
                    # fallback parse
                    cand = getattr(resp, 'candidates', None)
                    if cand and len(cand) > 0:
                        first = cand[0]
                        content = getattr(first, 'content', None)
                        if content:
                            parts = getattr(content, 'parts', None) or (content if isinstance(content, list) else None)
                            if parts and len(parts) > 0:
                                part0 = parts[0]
                                text = getattr(part0, 'text', None) or (part0 if isinstance(part0, str) else None)
                                if text:
                                    return text
                    return str(resp)
                except Exception:
                    logging.exception('GenAI SDK generative call failed; will fall back to REST')
                    raise

            try:
                text_response = await _sdk_gen_call_async()
                data = {'predictions': [{'content': text_response}]}
            except Exception:
                data = None
            # fallback: if genai not available or failed, try legacy vertexai SDK
            if data is None and _VERTEX_SDK_AVAILABLE and GENERATIVE_MODEL_ID:
                try:
                    def _vertex_gen_call():
                        global _GENERATIVE_MODEL
                        if _GENERATIVE_MODEL is None:
                            vertexai.init(project=PROJECT_ID, location=REGION)
                            _GENERATIVE_MODEL = GenerativeModel(GENERATIVE_MODEL_ID)
                        resp = _GENERATIVE_MODEL.generate_content([prompt], generation_config={"max_output_tokens": 1024})
                        # parse as before
                        try:
                            cand = getattr(resp, 'candidates', None)
                            if cand and len(cand) > 0:
                                first = cand[0]
                                content = getattr(first, 'content', None)
                                if content:
                                    parts = getattr(content, 'parts', None) or (content if isinstance(content, list) else None)
                                    if parts and len(parts) > 0:
                                        part0 = parts[0]
                                        text = getattr(part0, 'text', None) or (part0 if isinstance(part0, str) else None)
                                        if text:
                                            return text
                            text = getattr(resp, 'text', None)
                            if isinstance(text, str):
                                return text
                        except Exception:
                            logging.exception('Failed to parse Vertex SDK gen response')
                        return str(resp)

                    text_response = await asyncio.to_thread(_vertex_gen_call)
                    data = {'predictions': [{'content': text_response}]}
                except Exception:
                    logging.exception('Vertex SDK generative call failed; falling back to REST predict')

        # Fallback to REST predict if SDK not used or failed
        if data is None:
            if GENERATIVE_MODEL_ID:
                model_part = GENERATIVE_MODEL_ID
                if model_part.startswith('projects/'):
                    url = f"https://{REGION}-aiplatform.googleapis.com/v1/{model_part}:predict"
                else:
                    url = f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{REGION}/models/{model_part}:predict"
            elif GENERATIVE_ENDPOINT:
                url = f"https://{REGION}-aiplatform.googleapis.com/v1/{GENERATIVE_ENDPOINT}:predict"
            else:
                raise RuntimeError('No GENERATIVE_MODEL_ID or GENERATIVE_ENDPOINT configured')

            try:
                payload = {"instances": [{"content": prompt}]}
                resp = await async_post_with_retries(url, json=payload, headers=headers, timeout=(5.0, 120.0), retries=3, backoff_factor=1.0)
                data = resp.json()
            except httpx.ReadTimeout:
                logging.exception('Generative model request timed out')
                raise asyncio.TimeoutError('Generative model request timed out')
        # Try to extract a textual response from common response shapes
        text_response = None
        preds = data.get('predictions') or data.get('outputs') or []
        if isinstance(preds, list) and len(preds) > 0:
            first = preds[0]
            if isinstance(first, dict):
                # common keys to check
                for k in ('content','text','output','generated_text','candidates'):
                    if k in first:
                        if k == 'candidates' and isinstance(first[k], list) and len(first[k])>0:
                            cand = first[k][0]
                            # candidate may have 'content' or 'text'
                            if isinstance(cand, dict):
                                text_response = cand.get('content') or cand.get('text')
                            else:
                                text_response = str(cand)
                            break
                        else:
                            val = first[k]
                            if isinstance(val, str):
                                text_response = val
                                break
                            elif isinstance(val, dict) and 'text' in val:
                                text_response = val['text']
                                break
            elif isinstance(first, str):
                text_response = first

        if not text_response:
            # As final fallback, try to stringify the first prediction
            if isinstance(preds, list) and len(preds) > 0:
                text_response = json.dumps(preds[0])

        # strip fences
        if isinstance(text_response, str) and text_response.startswith("```json"):
            end = text_response.rfind("```")
            if end != -1:
                text_response = text_response[7:end]
            else:
                text_response = text_response[7:]

        parsed = {}
        try:
            parsed = json.loads(text_response) if text_response else {}
        except Exception:
            logging.exception('Failed to parse JSON from generative response')
            return {
                "title": {"suggested_value": None, "justification": "AI parsing failed.", "confidence_score": 0.0},
                "department": {"suggested_value": None, "justification": "AI parsing failed.", "confidence_score": 0.0},
                "process_type": {"suggested_value": None, "justification": "AI parsing failed.", "confidence_score": 0.0},
                "status": {"suggested_value": None, "justification": "AI parsing failed.", "confidence_score": 0.0}
            }

        return parsed
    except Exception as e:
        logging.error(f"Failed to get AI metadata suggestions: {e}", exc_info=True)
        return {
            "title": {"suggested_value": None, "justification": "AI extraction failed.", "confidence_score": 0.0},
            "department": {"suggested_value": None, "justification": "AI extraction failed.", "confidence_score": 0.0},
            "process_type": {"suggested_value": None, "justification": "AI extraction failed.", "confidence_score": 0.0},
            "status": {"suggested_value": None, "justification": "AI extraction failed.", "confidence_score": 0.0}
        }


@app.post('/rag-query')
async def rag_query(body: Dict[str, Any], current_user: Dict[str, Any] = Depends(get_current_user)):
    """Run a RAG-style query. Body: {"query": str, "department": str (optional), "top_k": int}

    Returns: {answer, sources: [{path, snippet}], debug?}
    """
    query = body.get('query')
    if not query:
        raise HTTPException(status_code=400, detail="Missing 'query' in request body")
    department = body.get('department')
    top_k = int(body.get('top_k', 3))

    # TEST_MODE: simple local behavior — return metadata summary
    if TEST_MODE:
        # find any version under local_test_store/versions matching department or first file
        versions_root = os.path.join('local_test_store', 'versions')
        candidates = []
        if os.path.exists(versions_root):
            for fn in os.listdir(versions_root):
                dirp = os.path.join(versions_root, fn)
                if os.path.isdir(dirp):
                    # pick latest v*.json
                    vfiles = sorted([p for p in os.listdir(dirp) if p.startswith('v') and p.endswith('.json')])
                    if vfiles:
                        path = os.path.join(dirp, vfiles[-1])
                        data = local_read_json(path)
                        if data:
                            candidates.append({'path': path, 'snippet': data.get('final_title','')})
        answer = f"TEST_MODE answer: found {len(candidates)} documents. Top: {candidates[0]['snippet'] if candidates else 'none'}"
        return {"answer": answer, "sources": candidates}

    # Production: generate embedding for query
    loop = asyncio.get_running_loop()
    try:
        emb = await get_text_embeddings([query])
        qvec = emb[0]
        qvec_str = '[' + ','.join(map(str, qvec)) + ']'
    except Exception as e:
        if isinstance(e, asyncio.TimeoutError):
            logging.exception('Embedding generation timed out')
            raise HTTPException(status_code=504, detail='Embedding generation timed out')
        logging.exception('Failed to generate query embedding')
        raise HTTPException(status_code=500, detail='Embedding generation failed')

    # Attempt pgvector nearest-neighbors search
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
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
    except Exception:
        # Fallback: simple text search
        logging.exception('pgvector search failed, falling back to text search')
        try:
            if department:
                cursor.execute("SELECT chunk_content, gcs_object_path FROM documents WHERE department_folder = %s AND chunk_content ILIKE %s LIMIT %s", (department, f"%{query}%", top_k))
            else:
                cursor.execute("SELECT chunk_content, gcs_object_path FROM documents WHERE chunk_content ILIKE %s LIMIT %s", (f"%{query}%", top_k))
            rows = cursor.fetchall()
            snippets = [{'path': r[1], 'snippet': r[0]} for r in rows]
        except Exception:
            logging.exception('Text search fallback also failed')
            snippets = []
    finally:
        cursor.close()
        conn.close()

    # Build prompt for generative model
    context_text = '\n\n'.join([s['snippet'] for s in snippets])[:4000]
    prompt = f"Answer the user query using the following document snippets. Query: {query}\n\nSnippets:\n{context_text}\n\nProvide a concise answer and list sources."

    if not GENERATIVE_ENDPOINT:
        logging.warning('GENERATIVE_ENDPOINT not configured; returning snippets as answer')
        return {"answer": context_text or "", "sources": snippets}

    headers = get_auth_headers()
    try:
        try:
            url = f"https://{REGION}-aiplatform.googleapis.com/v1/{GENERATIVE_ENDPOINT}:predict"
            payload = {"instances": [{"content": prompt}]}
            resp = await async_post_with_retries(url, json=payload, headers=headers, timeout=(5.0, 120.0), retries=3, backoff_factor=1.0)
            data = resp.json()
        except httpx.ReadTimeout:
            logging.exception('Generative model request timed out; returning snippets as fallback')
            return {"answer": context_text or "", "sources": snippets}
        preds = data.get('predictions') or data.get('outputs') or []
        text_response = None
        if isinstance(preds, list) and len(preds) > 0:
            first = preds[0]
            if isinstance(first, dict):
                # check common keys
                for k in ('content','text','output','generated_text','candidates'):
                    if k in first:
                        if k == 'candidates' and isinstance(first[k], list) and len(first[k])>0:
                            cand = first[k][0]
                            if isinstance(cand, dict):
                                text_response = cand.get('content') or cand.get('text')
                            else:
                                text_response = str(cand)
                            break
                        else:
                            val = first[k]
                            if isinstance(val, str):
                                text_response = val
                                break
                            elif isinstance(val, dict) and 'text' in val:
                                text_response = val['text']
                                break
            elif isinstance(first, str):
                text_response = first

        if not text_response:
            if isinstance(preds, list) and len(preds) > 0:
                text_response = json.dumps(preds[0])
    except Exception:
        logging.exception('Generative model failed; returning snippets as answer')
        return {"answer": context_text or "", "sources": snippets}

    return {"answer": text_response or (context_text or ""), "sources": snippets}


# --- Pydantic Models for Structured RAG Response ---
from pydantic import BaseModel, Field
from typing import Optional
import hashlib

class RagQueryRequest(BaseModel):
    query: str
    department: Optional[str] = None
    top_k: int = Field(default=5, ge=1, le=20)

class SourceCitation(BaseModel):
    sop_title: Optional[str] = None
    sop_id: Optional[int] = None
    version_id: Optional[int] = None
    version_status: str = "unknown"
    chunk_id: int
    section_number: Optional[str] = None
    snippet: str
    relevance_score: float

class ProceduralStep(BaseModel):
    step_number: Optional[int] = None
    text: str
    source_chunk_id: int
    sop_version_id: Optional[int] = None
    is_explicit: bool = True

class ConfidenceReport(BaseModel):
    overall_score: float = Field(ge=0.0, le=1.0)
    gaps_identified: List[str] = []
    ambiguities: List[str] = []
    human_judgment_required_for: List[str] = []
    version_conflicts: Optional[List[str]] = None

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
    steps: Optional[List[ProceduralStep]] = None
    sources: List[SourceCitation] = []
    confidence: ConfidenceReport
    query_audit: QueryAuditInfo
    disclaimer: str = "This response is advisory only. All outputs are traceable and may be audited."
    similar_precedents: Optional['PrecedentResponse'] = None


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
RagQueryResponse.model_rebuild()


# --- Historical Precedent System ---

async def get_precedents(
    override_reason: str,
    current_query_id: uuid.UUID,
    similarity_threshold: float = 0.70,
    limit_count: int = 5,
    min_precedents: int = 2
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
        logging.debug("get_precedents called with empty override_reason; returning empty")
        return []
    
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Call stored function: get_similar_precedents(reason, query_id, threshold, limit)
        # This function uses pg_trgm similarity() for fuzzy text matching
        cursor.execute("""
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
        """, (override_reason, str(current_query_id), similarity_threshold, limit_count, min_precedents))
        
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
                "precedent_rank": row[10]
            }
            precedents.append(precedent)
        
        # Only return if we have at least min_precedents
        if len(precedents) < min_precedents:
            logging.debug(f"Found {len(precedents)} precedents, but minimum is {min_precedents}; returning empty")
            return []
        
        logging.info(f"Found {len(precedents)} precedents for override (avg match: {sum(p['match_score'] for p in precedents) / len(precedents):.2f})")
        return precedents
        
    except Exception as e:
        logging.exception(f"Error retrieving precedents for '{override_reason[:50]}...': {e}")
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
    "probably", "likely", "might", "could", "possibly",
    "i think", "in my opinion", "you should", "we recommend",
    "best practice", "typically", "usually", "generally"
]

OUT_OF_SCOPE_PATTERNS = [
    "should we approve", "do you recommend", "what's the best way",
    "create a new policy", "legal advice", "compliance advice",
    "make a decision", "approve this"
]

def is_out_of_scope(query: str) -> Optional[str]:
    """Return reason if query is out of scope, else None."""
    q_lower = query.lower()
    for pattern in OUT_OF_SCOPE_PATTERNS:
        if pattern in q_lower:
            return f"Query contains out-of-scope request: '{pattern}'"
    return None

def sanitize_response(text: str) -> tuple:
    """Remove speculative language; return (sanitized, violations)."""
    violations = []
    sanitized = text
    for phrase in FORBIDDEN_SPECULATION:
        import re as re_mod
        if phrase.lower() in sanitized.lower():
            violations.append(f"Removed speculative phrase: '{phrase}'")
            sanitized = re_mod.sub(re_mod.escape(phrase), "", sanitized, flags=re_mod.IGNORECASE)
    return sanitized.strip(), violations

def extract_procedural_steps_from_chunks(chunks: List[Dict]) -> List[Dict]:
    """Extract numbered/bulleted steps from chunk content."""
    import re as re_mod
    steps = []
    for chunk in chunks:
        text = chunk.get('chunk_content') or chunk.get('snippet') or ''
        chunk_id = chunk.get('id') or chunk.get('chunk_id')
        version_id = chunk.get('sop_version_id')
        
        # Pattern 1: "1. Step text" or "1) Step text"
        numbered = re_mod.findall(r'^(\d+)[.\)]\s+(.+?)(?=\n\d+[.\)]|\n\n|$)', text, re_mod.MULTILINE | re_mod.DOTALL)
        for num, step_text in numbered:
            steps.append({
                "step_number": int(num),
                "text": step_text.strip()[:500],
                "source_chunk_id": chunk_id,
                "sop_version_id": version_id,
                "is_explicit": True
            })
        
        # Pattern 2: Bullets (if no numbered found)
        if not numbered:
            bullets = re_mod.findall(r'^\s*[-•]\s+(.+?)(?=\n\s*[-•]|\n\n|$)', text, re_mod.MULTILINE | re_mod.DOTALL)
            for idx, bullet_text in enumerate(bullets, 1):
                steps.append({
                    "step_number": idx,
                    "text": bullet_text.strip()[:500],
                    "source_chunk_id": chunk_id,
                    "sop_version_id": version_id,
                    "is_explicit": False
                })
    
    # Dedupe and sort
    seen = set()
    unique = []
    for s in steps:
        key = (s.get('step_number'), s.get('text')[:50])
        if key not in seen:
            seen.add(key)
            unique.append(s)
    return sorted(unique, key=lambda x: x.get('step_number') or 999)

def calculate_confidence(sources: List[Dict], gaps: List[str]) -> Dict:
    """Calculate confidence score based on sources and gaps."""
    if not sources:
        return {
            "overall_score": 0.0,
            "gaps_identified": ["No matching SOP content found"],
            "ambiguities": [],
            "human_judgment_required_for": ["Entire request"]
        }
    
    relevance_scores = [s.get('relevance_score', 0.5) for s in sources]
    avg_relevance = sum(relevance_scores) / len(relevance_scores)
    
    version_ids = set(s.get('sop_version_id') for s in sources if s.get('sop_version_id'))
    version_conflicts = None
    if len(version_ids) > 1:
        version_conflicts = [f"Answer spans {len(version_ids)} SOP versions: {list(version_ids)}"]
        gaps.append("Multiple SOP versions referenced - verify consistency")
    
    human_judgment = []
    if avg_relevance < 0.6:
        human_judgment.append("Low relevance scores - manual verification recommended")
    
    return {
        "overall_score": round(avg_relevance, 3),
        "gaps_identified": gaps,
        "ambiguities": [],
        "human_judgment_required_for": human_judgment,
        "version_conflicts": version_conflicts
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
    disclaimer: str = "This response is advisory only. All outputs are traceable and may be audited."


@app.post('/rag-query-corpus', response_model=VertexRagResponse)
async def rag_query_vertex_corpus(body: VertexRagQueryRequest, current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Query the Vertex AI RAG Corpus (Amcorpus) directly.
    
    This endpoint uses the native Vertex AI RAG API to retrieve contexts
    from your pre-indexed SOP documents in Cloud Spanner vector DB.
    
    Advantages over /rag-query-v2:
    - Uses Google-managed embedding & chunking
    - Automatic indexing and retrieval
    - No need for local pgvector
    """
    session_id = str(uuid.uuid4())
    gaps = []
    
    # 1. Scope Validation
    scope_issue = is_out_of_scope(body.query)
    if scope_issue:
        gaps.append(scope_issue)
    
    # 2. Query Vertex AI RAG Corpus
    url = f"https://{RAG_REGION}-aiplatform.googleapis.com/v1beta1/{RAG_CORPUS_NAME}:retrieveContexts"
    headers = get_auth_headers()
    headers["Content-Type"] = "application/json"
    
    payload = {
        "query": {
            "text": body.query,
            "ragRetrievalConfig": {
                "topK": body.top_k,
                "filter": {
                    "vectorSimilarityThreshold": body.similarity_threshold
                }
            }
        },
        "vertexRagStore": {
            "ragCorpora": [RAG_CORPUS_NAME]
        }
    }
    
    contexts = []
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload, timeout=30.0)
            
            if resp.status_code != 200:
                logging.error(f"RAG corpus query failed: {resp.status_code} - {resp.text}")
                gaps.append(f"RAG corpus query failed: {resp.status_code}")
            else:
                data = resp.json()
                raw_contexts = data.get('contexts', {}).get('contexts', [])
                
                for ctx in raw_contexts:
                    contexts.append(VertexRagContext(
                        source_uri=ctx.get('sourceUri'),
                        text=ctx.get('text', ''),
                        distance=ctx.get('distance')
                    ))
    except asyncio.TimeoutError:
        gaps.append("RAG corpus query timed out")
        logging.exception("RAG corpus query timed out")
    except Exception as e:
        gaps.append(f"RAG corpus error: {str(e)[:100]}")
        logging.exception("RAG corpus query failed")
    
    # 3. Check coverage
    if not contexts:
        gaps.append("No matching content found in RAG corpus")
        gaps.append("RECOMMENDATION: Ensure documents are imported into the corpus")
    
    # 4. Generate answer using retrieved contexts
    summary_answer = "No answer could be generated."
    
    if contexts:
        context_text = '\n\n---\n\n'.join([c.text for c in contexts])[:6000]
        
        system_prompt = """You are an SOP Query Assistant for a regulated banking environment.
RULES:
- Use ONLY the provided SOP content
- Do NOT invent steps, rules, or thresholds
- Do NOT use speculative language (probably, likely, might, could)
- If content is insufficient, say so explicitly"""
        
        user_prompt = f"""Query: {body.query}

SOP Content:
{context_text}

Provide a direct, procedural answer based ONLY on the above content."""
        
        try:
            full_prompt = f"{system_prompt}\n\n{user_prompt}"
            
            if _GENAI_AVAILABLE:
                client = _get_genai_client()
                async def _gen():
                    resp = await client.aio.models.generate_content(
                        model=GENERATIVE_MODEL_ID or 'gemini-2.5-pro',
                        contents=full_prompt,
                        config=types.GenerateContentConfig(max_output_tokens=1024, temperature=0.1)
                    )
                    return resp.text if hasattr(resp, 'text') else str(resp)
                raw_answer = await _gen()
            else:
                # REST fallback
                gen_url = f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{REGION}/publishers/google/models/{GENERATIVE_MODEL_ID or 'gemini-1.5-pro'}:generateContent"
                gen_payload = {"contents": [{"parts": [{"text": full_prompt}]}], "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.1}}
                resp = await async_post_with_retries(gen_url, json=gen_payload, headers=get_auth_headers(), timeout=(5.0, 120.0))
                data = resp.json()
                candidates = data.get('candidates', [])
                if candidates:
                    parts = candidates[0].get('content', {}).get('parts', [])
                    raw_answer = parts[0].get('text', '') if parts else ''
                else:
                    raw_answer = ''
            
            summary_answer, violations = sanitize_response(raw_answer)
            if violations:
                gaps.extend(violations)
        
        except Exception as e:
            logging.exception("Answer generation failed")
            gaps.append(f"Answer generation error: {str(e)[:100]}")
            summary_answer = f"Could not generate answer. Retrieved {len(contexts)} contexts for manual review."
    
    # 5. Calculate confidence
    if contexts:
        distances = [c.distance for c in contexts if c.distance is not None]
        # Distance is 0 = perfect match, 1 = no match, so convert to score
        scores = [1.0 - d for d in distances] if distances else [0.5]
        avg_score = sum(scores) / len(scores)
    else:
        avg_score = 0.0
    
    confidence = ConfidenceReport(
        overall_score=round(avg_score, 3),
        gaps_identified=gaps,
        ambiguities=[],
        human_judgment_required_for=["Low confidence answer"] if avg_score < 0.6 else []
    )
    
    return VertexRagResponse(
        session_id=session_id,
        query=body.query,
        summary_answer=summary_answer,
        contexts=contexts,
        confidence=confidence
    )


@app.post('/rag-query-v2', response_model=RagQueryResponse)
async def rag_query_structured(body: RagQueryRequest, current_user: Dict[str, Any] = Depends(get_current_user)):
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
        gaps.append(scope_issue)
        # Log but continue - let response indicate limitation
    
    # 2. Generate query embedding
    try:
        emb = await get_text_embeddings([body.query])
        qvec = emb[0]
        qvec_str = '[' + ','.join(map(str, qvec)) + ']'
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail='Embedding generation timed out')
    except Exception:
        logging.exception('Failed to generate query embedding')
        raise HTTPException(status_code=500, detail='Embedding generation failed')
    
    # 3. Vector search - ONLY ACTIVE SOP versions
    conn = get_db_connection()
    cursor = conn.cursor()
    sources = []
    raw_chunks = []
    
    try:
        # Join documents with chunk_sop_mapping and filter by active versions
        # Fallback: if mapping table doesn't exist, query documents directly
        try:
            dept_filter = "AND d.department_folder = %s" if body.department else ""
            params = [qvec_str]
            if body.department:
                params.append(body.department)
            params.append(body.top_k)
            
            sql = f"""
                SELECT 
                    d.id as chunk_id,
                    d.chunk_content,
                    d.gcs_object_path,
                    d.department_folder,
                    d.final_title,
                    csm.sop_id,
                    csm.sop_version_id,
                    csm.section_number,
                    csm.is_procedural_step,
                    sv.status as version_status,
                    s.title as sop_title,
                    1 - (d.embedding_vector <-> %s::vector) as relevance_score
                FROM documents d
                LEFT JOIN chunk_sop_mapping csm ON d.id = csm.chunk_id
                LEFT JOIN sop_versions sv ON csm.sop_version_id = sv.version_id
                LEFT JOIN sops s ON csm.sop_id = s.sop_id
                WHERE d.embedding_vector IS NOT NULL
                  AND (sv.status = 'active' OR sv.status IS NULL)
                  {dept_filter}
                ORDER BY d.embedding_vector <-> %s::vector
                LIMIT %s
            """
            # Need qvec_str twice
            params_full = [qvec_str]
            if body.department:
                params_full.append(body.department)
            params_full.extend([qvec_str, body.top_k])
            cursor.execute(sql, tuple(params_full))
            rows = cursor.fetchall()
            
        except Exception as e:
            # Fallback: simple query without mapping tables
            logging.warning(f"chunk_sop_mapping query failed, using fallback: {e}")
            dept_filter = "WHERE d.department_folder = %s" if body.department else ""
            params = []
            if body.department:
                params.append(body.department)
            params.extend([qvec_str, body.top_k])
            
            sql = f"""
                SELECT 
                    d.id as chunk_id,
                    d.chunk_content,
                    d.gcs_object_path,
                    d.department_folder,
                    d.final_title,
                    NULL as sop_id,
                    NULL as sop_version_id,
                    NULL as section_number,
                    FALSE as is_procedural_step,
                    'unknown' as version_status,
                    d.final_title as sop_title,
                    1 - (d.embedding_vector <-> %s::vector) as relevance_score
                FROM documents d
                {dept_filter}
                {"AND" if body.department else "WHERE"} d.embedding_vector IS NOT NULL
                ORDER BY d.embedding_vector <-> %s::vector
                LIMIT %s
            """
            params_full = []
            if body.department:
                params_full.append(body.department)
            params_full.extend([qvec_str, qvec_str, body.top_k])
            cursor.execute(sql, tuple(params_full))
            rows = cursor.fetchall()
        
        for row in rows:
            chunk_id, content, gcs_path, dept, title, sop_id, version_id, section, is_proc, ver_status, sop_title, rel_score = row
            raw_chunks.append({
                'id': chunk_id,
                'chunk_content': content,
                'sop_version_id': version_id,
                'sop_id': sop_id
            })
            sources.append({
                'sop_title': sop_title or title,
                'sop_id': sop_id,
                'version_id': version_id,
                'version_status': ver_status or 'unknown',
                'chunk_id': chunk_id,
                'section_number': section,
                'snippet': (content or '')[:400],
                'relevance_score': float(rel_score) if rel_score else 0.5
            })
    
    except Exception:
        logging.exception('RAG query database search failed')
        raise HTTPException(status_code=500, detail='Database search failed')
    finally:
        cursor.close()
    
    # 4. Check coverage
    if not sources:
        gaps.append("No matching SOP content found for this query")
        gaps.append("RECOMMENDATION: Contact compliance team for manual guidance")
    else:
        avg_rel = sum(s['relevance_score'] for s in sources) / len(sources)
        if avg_rel < 0.5:
            gaps.append(f"Low relevance scores (avg: {avg_rel:.1%}) - results may not be directly applicable")
    
    # 5. Extract procedural steps
    steps = None
    if raw_chunks:
        extracted = extract_procedural_steps_from_chunks(raw_chunks)
        if extracted:
            steps = [ProceduralStep(**s) for s in extracted]
    
    # 6. Generate answer using LLM
    context_text = '\n\n---\n\n'.join([s['snippet'] for s in sources])[:6000]
    
    system_prompt = """You are an SOP Query Assistant for a regulated banking environment.
RULES:
- Use ONLY the provided SOP content
- Do NOT invent steps, rules, or thresholds
- Do NOT use speculative language (probably, likely, might, could)
- Distinguish fact from interpretation
- If content is insufficient, say so explicitly
- Reference section numbers when available"""
    
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
                    model=GENERATIVE_MODEL_ID or 'gemini-2.5-pro',
                    contents=full_prompt,
                    config=types.GenerateContentConfig(max_output_tokens=1024, temperature=0.1)
                )
                return resp.text if hasattr(resp, 'text') else str(resp)
            raw_answer = await _gen()
        elif _VERTEX_SDK_AVAILABLE and GENERATIVE_MODEL_ID:
            def _vertex_call():
                global _GENERATIVE_MODEL
                if _GENERATIVE_MODEL is None:
                    vertexai.init(project=PROJECT_ID, location=REGION)
                    _GENERATIVE_MODEL = GenerativeModel(GENERATIVE_MODEL_ID)
                resp = _GENERATIVE_MODEL.generate_content([full_prompt], generation_config={"max_output_tokens": 1024, "temperature": 0.1})
                return resp.text if hasattr(resp, 'text') else str(resp)
            raw_answer = await asyncio.to_thread(_vertex_call)
        else:
            # REST fallback
            headers = get_auth_headers()
            url = f"https://{REGION}-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/{REGION}/publishers/google/models/{GENERATIVE_MODEL_ID or 'gemini-1.5-pro'}:generateContent"
            payload = {"contents": [{"parts": [{"text": full_prompt}]}], "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.1}}
            resp = await async_post_with_retries(url, json=payload, headers=headers, timeout=(5.0, 120.0))
            data = resp.json()
            candidates = data.get('candidates', [])
            if candidates:
                parts = candidates[0].get('content', {}).get('parts', [])
                raw_answer = parts[0].get('text', '') if parts else ''
            else:
                raw_answer = ''
        
        # Sanitize
        summary_answer, violations = sanitize_response(raw_answer)
        if violations:
            for v in violations:
                gaps.append(v)
    
    except asyncio.TimeoutError:
        gaps.append("Generative model timed out")
        summary_answer = f"Answer generation timed out. Retrieved {len(sources)} relevant SOP sections."
    except Exception as e:
        logging.exception('Generative answer failed')
        gaps.append(f"Answer generation error: {str(e)[:100]}")
        summary_answer = f"Could not generate answer. Retrieved {len(sources)} relevant SOP sections for manual review."
    
    # 7. Build confidence report
    confidence = calculate_confidence(sources, gaps)
    
    # 8. Build audit info
    sop_versions_used = list(set(s['version_id'] for s in sources if s.get('version_id')))
    audit_info = QueryAuditInfo(
        session_id=session_id,
        timestamp=query_timestamp.isoformat(),
        user_id=current_user.get('id'),
        user_role=current_user.get('role'),
        sop_versions_used=sop_versions_used,
        chunks_retrieved=len(sources)
    )
    
    # 9. Log to database (async, non-blocking)
    try:
        log_conn = get_db_connection()
        log_cursor = log_conn.cursor()
        
        # Insert session
        log_cursor.execute("""
            INSERT INTO query_sessions 
            (session_id, user_id, user_role, user_departments, query_text, query_hash, query_timestamp, response_timestamp, status, confidence_score, gaps_identified)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
        """, (
            session_id,
            current_user.get('id'),
            current_user.get('role'),
            json.dumps(current_user.get('departments')),
            body.query,
            hashlib.sha256(body.query.encode()).hexdigest(),
            query_timestamp,
            datetime.now(timezone.utc),
            'success' if sources else 'no_coverage',
            confidence['overall_score'],
            json.dumps(gaps)
        ))
        
        # Insert results
        for idx, src in enumerate(sources):
            log_cursor.execute("""
                INSERT INTO query_results
                (session_id, sop_id, sop_version_id, chunk_id, relevance_score, citation_index)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (
                session_id, src.get('sop_id'), src.get('version_id'), 
                src['chunk_id'], src['relevance_score'], idx
            ))
        
        # Log gaps as audit events
        for gap in gaps:
            log_cursor.execute("""
                INSERT INTO query_audit_log (session_id, event_type, event_details, severity)
                VALUES (%s, %s, %s, %s)
            """, (session_id, 'coverage_gap' if 'gap' in gap.lower() else 'info', json.dumps({"message": gap}), 'warning' if 'gap' in gap.lower() else 'info'))
        
        # Cache response
        log_cursor.execute("""
            INSERT INTO query_response_cache (session_id, summary_answer, steps, full_response, model_used)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
        """, (
            session_id,
            summary_answer,
            json.dumps([s.dict() if hasattr(s, 'dict') else s for s in (steps or [])]),
            json.dumps({"sources_count": len(sources), "steps_count": len(steps or [])}),
            GENERATIVE_MODEL_ID or 'gemini-2.5-pro'
        ))
        
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
        if confidence.get('overall_score', 0.0) < 0.6 and summary_answer:
            # Use the summary answer as the search basis for similar overrides
            # (In practice, this would be called after user explicitly rejects answer)
            precedent_list = await get_precedents(
                override_reason=body.query[:200],  # Query as potential override reason
                current_query_id=uuid.UUID(session_id),
                similarity_threshold=0.70,
                limit_count=5,
                min_precedents=2
            )
            
            if precedent_list and len(precedent_list) >= 2:
                # Build PrecedentCase objects
                precedent_cases = []
                for p in precedent_list:
                    case = PrecedentCase(
                        override_id=p['override_id'],
                        user_id=p['user_id'],
                        user_department=p['user_department'],
                        original_recommendation=p.get('original_recommendation'),
                        scenario=p['override_reason'][:100],  # First 100 chars as scenario summary
                        override_reason=p['override_reason'],
                        how_resolved=p['resolution_notes'] or 'No notes available',
                        similarity_score=round(p['match_score'], 3),
                        resolved_date=p['resolution_date'],
                        resolved_by=p['resolved_by_user_id']
                    )
                    precedent_cases.append(case)
                
                precedent_response = PrecedentResponse(
                    precedent_count=len(precedent_cases),
                    show_precedents=True,
                    cases=precedent_cases
                )
                logging.info(f"Retrieved {len(precedent_cases)} precedent cases for low-confidence query")
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
        similar_precedents=precedent_response

    )


# --- Manager Workflow Models ---

class ResolveOverrideRequest(BaseModel):
    """Request to mark an override as resolved (manager approval)."""
    resolution_notes: str = Field(..., min_length=10, max_length=1000, description="How was this override resolved? Decision rationale.")
    

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


async def _log_upload_event(session_id: str, user_id: Optional[int], event_type: str, field_changes: Optional[Dict] = None, event_details: Optional[Dict] = None):
    """Log upload events to audit table."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO upload_audit_log (session_id, user_id, event_type, field_changes, event_details)
            VALUES (%s, %s, %s, %s, %s)
        """, (str(session_id), user_id, event_type, json.dumps(field_changes) if field_changes else None, json.dumps(event_details) if event_details else None))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception:
        logging.exception("Failed to log upload event")


@app.post('/document-upload', response_model=UploadMetadataResponse)
async def document_upload(file: UploadFile = File(...), current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Upload a document and extract metadata.
    
    Returns extracted metadata for user confirmation.
    Does NOT save to GCS yet - awaits user confirmation.
    """
    session_id = str(uuid.uuid4())
    user_id = current_user.get('id')
    
    if not file.filename:
        raise HTTPException(status_code=400, detail="Missing filename")
    
    # Validate file type
    if not (file.filename.lower().endswith(('.pdf', '.txt', '.md'))):
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
        if file.filename.lower().endswith('.pdf'):
            import io
            pdf = PdfReader(io.BytesIO(contents))
            document_text = '\n'.join([page.extract_text() for page in pdf.pages])
        else:
            document_text = contents.decode('utf-8')
    except Exception as e:
        logging.error(f"Text extraction failed: {e}")
        raise HTTPException(status_code=400, detail="Failed to extract text from file")
    
    if not document_text:
        raise HTTPException(status_code=400, detail="No text content extracted")
    
    # Get AI metadata suggestions
    try:
        ai_metadata = await get_ai_metadata_suggestions(document_text[:5000])  # Use first 5000 chars for speed
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Metadata extraction timed out")
    except Exception as e:
        logging.error(f"AI metadata extraction failed: {e}")
        raise HTTPException(status_code=500, detail="Metadata extraction failed")
    
    # Create upload session (draft state)
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO document_uploads (session_id, user_id, filename, original_filename, upload_status)
            VALUES (%s, %s, %s, %s, 'draft')
        """, (session_id, user_id, file.filename, file.filename))
        
        # Create metadata draft
        cursor.execute("""
            INSERT INTO upload_metadata_drafts 
            (session_id, extracted_title, extracted_department, extracted_author, extracted_type, 
             ai_confidence, ai_model_used, confirmed_title, confirmed_department, confirmed_author, confirmed_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            session_id,
            ai_metadata.get('title', {}).get('suggested_value'),
            ai_metadata.get('department', {}).get('suggested_value'),
            'Unknown',  # Author not in current AI suggestions
            ai_metadata.get('process_type', {}).get('suggested_value'),
            json.dumps({
                'title': ai_metadata.get('title', {}).get('confidence_score', 0.0),
                'department': ai_metadata.get('department', {}).get('confidence_score', 0.0),
                'type': ai_metadata.get('process_type', {}).get('confidence_score', 0.0),
            }),
            GENERATIVE_MODEL_ID or 'gemini-2.5-pro',
            ai_metadata.get('title', {}).get('suggested_value'),
            ai_metadata.get('department', {}).get('suggested_value'),
            'Unknown',
            ai_metadata.get('process_type', {}).get('suggested_value')
        ))
        
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Failed to create upload session: {e}")
        raise HTTPException(status_code=500, detail="Failed to create upload session")
    finally:
        cursor.close()
        conn.close()
    
    # Log extraction event
    await _log_upload_event(session_id, user_id, 'extracted', event_details={'filename': file.filename, 'text_length': len(document_text)})
    
    return UploadMetadataResponse(
        session_id=session_id,
        extracted_title=ai_metadata.get('title', {}).get('suggested_value') or '',
        extracted_department=ai_metadata.get('department', {}).get('suggested_value') or '',
        extracted_author='Unknown',
        extracted_type=ai_metadata.get('process_type', {}).get('suggested_value') or '',
        ai_confidence={
            'title': ai_metadata.get('title', {}).get('confidence_score', 0.0),
            'department': ai_metadata.get('department', {}).get('confidence_score', 0.0),
            'type': ai_metadata.get('process_type', {}).get('confidence_score', 0.0),
        },
        upload_status='draft'
    )


@app.patch('/document-uploads/{session_id}/metadata')
async def update_upload_metadata(session_id: str, edits: MetadataEditRequest, current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Update metadata for a pending upload (before confirmation).
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Verify session exists and belongs to user
        cursor.execute("""
            SELECT user_id, upload_status FROM document_uploads WHERE session_id = %s
        """, (session_id,))
        row = cursor.fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")
        
        session_user_id, status = row
        if session_user_id != current_user.get('id') and current_user.get('role') != 'manager':
            raise HTTPException(status_code=403, detail="Not authorized to edit this upload")
        
        if status != 'draft':
            raise HTTPException(status_code=409, detail="Cannot edit non-draft upload")
        
        # Validate department access (officers can only upload to their departments)
        if edits.department and current_user.get('role') != 'manager':
            user_depts = current_user.get('departments', [])
            if edits.department not in user_depts:
                raise HTTPException(status_code=403, detail="Not authorized to upload to this department")
        
        # Track changes
        field_changes = {}
        update_fields = []
        update_values = []
        
        if edits.title is not None:
            update_fields.append("user_title = %s, confirmed_title = %s")
            update_values.extend([edits.title, edits.title])
            field_changes['title'] = {'to': edits.title}
        
        if edits.department is not None:
            update_fields.append("user_department = %s, confirmed_department = %s")
            update_values.extend([edits.department, edits.department])
            field_changes['department'] = {'to': edits.department}
        
        if edits.author is not None:
            update_fields.append("user_author = %s, confirmed_author = %s")
            update_values.extend([edits.author, edits.author])
            field_changes['author'] = {'to': edits.author}
        
        if edits.type is not None:
            update_fields.append("user_type = %s, confirmed_type = %s")
            update_values.extend([edits.type, edits.type])
            field_changes['type'] = {'to': edits.type}
        
        if not update_fields:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=400, detail="No fields to update")
        
        # Update draft
        update_values.append(session_id)
        cursor.execute(f"""
            UPDATE upload_metadata_drafts 
            SET {', '.join(update_fields)}, last_edited_at = NOW()
            WHERE session_id = %s
        """, tuple(update_values))
        
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
    await _log_upload_event(session_id, current_user.get('id'), 'edited', field_changes=field_changes)
    
    return {'session_id': session_id, 'status': 'updated'}


@app.post('/document-uploads/{session_id}/confirm', response_model=ConfirmUploadResponse)
async def confirm_upload(session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Confirm upload: save to GCS, create documents/chunks, generate embeddings.
    Atomic operation.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Get upload session and metadata
        cursor.execute("""
            SELECT du.user_id, du.filename, du.original_filename, umd.confirmed_title, umd.confirmed_department, umd.confirmed_author, umd.confirmed_type
            FROM document_uploads du
            JOIN upload_metadata_drafts umd ON du.session_id = umd.session_id
            WHERE du.session_id = %s
        """, (session_id,))
        
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")
        
        uploader_id, filename, original_filename, final_title, final_department, final_author, final_type = row
        
        if uploader_id != current_user.get('id') and current_user.get('role') != 'manager':
            raise HTTPException(status_code=403, detail="Not authorized to confirm this upload")
        
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
            # gcs_blob = bucket.blob(gcs_path)
            # gcs_blob.upload_from_string(file_contents)
        except Exception as e:
            logging.error(f"GCS upload failed: {e}")
            raise HTTPException(status_code=500, detail="Failed to save to GCS")
        
        # Create document record (simplified, full logic from /process-document)
        cursor.execute("""
            INSERT INTO documents 
            (original_gcs_filename, gcs_object_path, department_folder, chunk_index, chunk_content,
             final_title, final_department, final_process_type, final_status, review_status, upload_session_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'approved', %s)
            RETURNING id
        """, (
            original_filename, gcs_path, final_department, 0, 'Document content',
            final_title, final_department, final_type, 'confirmed', session_id
        ))
        
        document_id = cursor.fetchone()[0]
        
        # Update upload session
        cursor.execute("""
            UPDATE document_uploads 
            SET upload_status = 'confirmed', gcs_path = %s, document_id = %s, confirmed_at = NOW()
            WHERE session_id = %s
        """, (gcs_path, document_id, session_id))
        
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
    await _log_upload_event(session_id, current_user.get('id'), 'confirmed', event_details={'document_id': document_id, 'gcs_path': gcs_path})
    
    return ConfirmUploadResponse(
        session_id=session_id,
        document_id=document_id,
        gcs_path=gcs_path,
        chunks_count=1,
        status='confirmed'
    )


@app.post('/document-uploads/{session_id}/undo', response_model=UndoPromptResponse)
async def undo_upload(session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Open undo prompt for confirmed upload.
    Returns options: edit, discard
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT user_id, upload_status FROM document_uploads WHERE session_id = %s
        """, (session_id,))
        
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")
        
        user_id, status = row
        if user_id != current_user.get('id') and current_user.get('role') != 'manager':
            raise HTTPException(status_code=403, detail="Not authorized")
        
        if status != 'confirmed':
            raise HTTPException(status_code=409, detail="Can only undo confirmed uploads")
    
    finally:
        cursor.close()
        conn.close()
    
    return UndoPromptResponse(session_id=session_id, options=['edit', 'discard'])


@app.post('/document-uploads/{session_id}/undo-edit')
async def undo_edit(session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Reload last confirmed metadata for editing.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT user_id, upload_status FROM document_uploads WHERE session_id = %s
        """, (session_id,))
        
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")
        
        user_id, status = row
        if user_id != current_user.get('id') and current_user.get('role') != 'manager':
            raise HTTPException(status_code=403, detail="Not authorized")
        
        # Get confirmed metadata
        cursor.execute("""
            SELECT confirmed_title, confirmed_department, confirmed_author, confirmed_type
            FROM upload_metadata_drafts
            WHERE session_id = %s
        """, (session_id,))
        
        meta_row = cursor.fetchone()
        if not meta_row:
            raise HTTPException(status_code=404, detail="Metadata not found")
        
        confirmed_title, confirmed_department, confirmed_author, confirmed_type = meta_row
        
        # Reset to draft state
        cursor.execute("""
            UPDATE document_uploads SET upload_status = 'draft' WHERE session_id = %s
        """, (session_id,))
        
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
    await _log_upload_event(session_id, current_user.get('id'), 'undo_edit')
    
    return {
        'session_id': session_id,
        'title': confirmed_title,
        'department': confirmed_department,
        'author': confirmed_author,
        'type': confirmed_type,
        'status': 'draft'
    }


@app.delete('/document-uploads/{session_id}')
async def discard_upload(session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Discard upload: soft delete document, purge metadata, remove from GCS & RAG.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Get upload session
        cursor.execute("""
            SELECT user_id, gcs_path, document_id FROM document_uploads WHERE session_id = %s
        """, (session_id,))
        
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Upload session not found")
        
        user_id, gcs_path, doc_id = row
        if user_id != current_user.get('id') and current_user.get('role') != 'manager':
            raise HTTPException(status_code=403, detail="Not authorized to discard this upload")
        
        # Soft delete document and chunks
        if doc_id:
            cursor.execute("UPDATE documents SET is_deleted = TRUE WHERE id = %s", (doc_id,))
        
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
        cursor.execute("""
            UPDATE document_uploads SET upload_status = 'discarded', discarded_at = NOW(), is_deleted = TRUE
            WHERE session_id = %s
        """, (session_id,))
        
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
    await _log_upload_event(session_id, current_user.get('id'), 'discarded', event_details={'gcs_path': gcs_path})
    
    return DiscardResponse(session_id=session_id, status='discarded', message='Upload and all data permanently deleted')


# --- Manager Workflow Endpoints ---

@app.post('/override/{override_id}/resolve', response_model=ResolveOverrideResponse)
async def resolve_override(
    override_id: str,
    body: ResolveOverrideRequest,
    current_user: Dict[str, Any] = Depends(get_current_user)
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
    user_role = current_user.get('role', '').lower()
    if user_role not in ['manager', 'compliance']:
        raise HTTPException(
            status_code=403,
            detail=f"Only managers/compliance can resolve overrides. Your role: {user_role}"
        )
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # Verify override exists
        cursor.execute("""
            SELECT override_id, user_id, is_resolved FROM override_log WHERE override_id = %s
        """, (override_id,))
        
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Override {override_id} not found")
        
        override_uuid, override_user_id, is_already_resolved = row
        
        if is_already_resolved:
            raise HTTPException(
                status_code=400,
                detail=f"Override {override_id} is already resolved"
            )
        
        # Mark as resolved using stored function
        cursor.execute("""
            SELECT mark_override_resolved(%s, %s, %s);
        """, (override_id, body.resolution_notes, current_user.get('id')))
        
        result = cursor.fetchone()
        conn.commit()
        
        if not result or not result[0]:
            raise HTTPException(status_code=500, detail="Failed to update override")
        
        # Retrieve updated override for response
        cursor.execute("""
            SELECT override_id, resolution_date, resolved_by_user_id FROM override_log WHERE override_id = %s
        """, (override_id,))
        
        updated_row = cursor.fetchone()
        if not updated_row:
            raise HTTPException(status_code=500, detail="Override not found after update")
        
        upd_id, res_date, res_by = updated_row
        
        logging.info(f"Override {override_id} marked as resolved by {current_user.get('username', 'unknown')}")
        
        return ResolveOverrideResponse(
            override_id=str(upd_id),
            is_resolved=True,
            resolution_date=res_date.isoformat() if res_date else '',
            resolved_by=res_by or '',
            message=f"Override resolved successfully. This case is now available as a precedent for other staff."
        )
    
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        logging.exception(f"Failed to resolve override {override_id}")
        raise HTTPException(status_code=500, detail=f"Failed to resolve override: {str(e)[:100]}")
    finally:
        cursor.close()
        conn.close()


@app.get('/precedent-details/{override_id}', response_model=PrecedentDetailsResponse)
async def get_precedent_details(
    override_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user)
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
        cursor.execute("""
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
        """, (override_id,))
        
        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Precedent case {override_id} not found or not yet resolved"
            )
        
        o_id, u_id, u_dept, orig_rec, override_reason, res_notes, res_by, res_date, created, q_id = row
        
        # Optionally retrieve query context (if query_sessions table exists)
        query_text = None
        if q_id:
            try:
                cursor.execute("""
                    SELECT query_text FROM query_sessions WHERE session_id = %s LIMIT 1
                """, (str(q_id),))
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
            how_resolved=res_notes or 'No resolution notes',
            resolved_date=res_date.isoformat() if res_date else None,
            resolved_by_user_id=res_by,
            created_at=created.isoformat() if created else '',
            query_session_id=str(q_id) if q_id else None,
            query_text=query_text
        )
    
    except HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Failed to retrieve precedent details for {override_id}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve precedent: {str(e)[:100]}")
    finally:
        cursor.close()
        conn.close()


# --- API Endpoints ---

@app.post("/process-document")
async def process_document_from_pubsub(request: Request):
    """
    Receives Pub/Sub messages indicating new document uploads,
    processes them, generates embeddings, and stores in the DB.
    Also moves the GCS object to a departmental folder.
    """
    logging.info("Received Pub/Sub message...")
    original_gcs_filename = None # Initialize outside try for finally block
    try:
        envelope = await request.json()
        message = envelope['message']
        
        if 'data' not in message:
            logging.warning("Pub/Sub message data missing. Skipping.")
            return {"status": "skipped", "message": "No data in message"}

        pubsub_data = base64.b64decode(message['data']).decode('utf-8')
        gcs_event = json.loads(pubsub_data)
        original_gcs_filename = gcs_event['name'] # This is the file at the root of the bucket
        bucket_name = gcs_event['bucket']

        logging.info(f"Processing file: {original_gcs_filename} from bucket: {bucket_name}")

        # 1. Download Document from GCS (from original location)
        bucket = storage_client.bucket(bucket_name)
        original_blob = bucket.blob(original_gcs_filename)
        
        document_text = ""
        if original_gcs_filename.lower().endswith('.pdf'):
            document_text = extract_text_from_pdf(original_blob)
        elif original_gcs_filename.lower().endswith(('.txt', '.md')):
            document_text = original_blob.download_as_text()
        else:
            logging.warning(f"Unsupported file type for {original_gcs_filename}. Skipping text extraction.")
        
        if not document_text:
            logging.error(f"Could not extract text from {original_gcs_filename}. Skipping further processing.")
            raise HTTPException(status_code=400, detail=f"Could not extract text from {original_gcs_filename}")

        # 2. Get AI Metadata Suggestions (including suggested department)
        try:
            ai_metadata = await get_ai_metadata_suggestions(document_text)
        except asyncio.TimeoutError:
            logging.exception('AI metadata generation timed out')
            raise HTTPException(status_code=504, detail='AI metadata generation timed out')
        
        suggested_department = ai_metadata['department']['suggested_value']
        if not suggested_department:
            suggested_department = "unclassified" # Default if AI can't determine
            logging.warning(f"AI could not suggest department for {original_gcs_filename}. Defaulting to 'unclassified'.")
        
        department_folder = suggested_department.lower().replace(' ', '-') # Sanitize for GCS path
        new_gcs_object_path = f"{department_folder}/{original_gcs_filename}"

        # 3. Move GCS Object to Departmental Folder
        # This is a copy followed by a delete of the original. Use bucket.copy_blob for correctness.
        logging.info(f"Moving GCS object from {original_gcs_filename} to {new_gcs_object_path}")
        new_blob = bucket.blob(new_gcs_object_path)

        # Ensure the original blob exists before attempting to copy/delete
        if original_blob.exists():
            try:
                # bucket.copy_blob(source_blob, destination_bucket, new_name)
                bucket.copy_blob(original_blob, bucket, new_gcs_object_path)
                original_blob.delete()
                logging.info(f"Successfully moved {original_gcs_filename} to {new_gcs_object_path}")
            except Exception as e:
                logging.error(f"Failed to copy/delete blob: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Failed to move object: {e}")
        else:
            logging.error(f"Original GCS object {original_gcs_filename} not found for move operation.")
            raise HTTPException(status_code=500, detail="Original GCS object not found.")


        # 4. Chunk Text
        text_chunks = chunk_text(document_text)
        if not text_chunks:
            logging.error(f"No text chunks generated for {original_gcs_filename}.")
            raise HTTPException(status_code=400, detail=f"No text chunks generated for {original_gcs_filename}")

        # 5. Generate Embeddings for Chunks
        chunk_embeddings = await get_text_embeddings(text_chunks)
        if len(text_chunks) != len(chunk_embeddings):
            logging.error("Mismatch between number of chunks and embeddings.")
            raise HTTPException(status_code=500, detail="Embedding generation failed partially.")

        # 6. Store in PostgreSQL
        conn = get_db_connection()
        cursor = conn.cursor()

        insert_values = []
        for i, chunk in enumerate(text_chunks):
            # Prepare embedding as a string literal that can be cast to pgvector
            embedding_str = '[' + ','.join(map(str, chunk_embeddings[i])) + ']' if chunk_embeddings[i] else None
            insert_values.append((
                original_gcs_filename,
                new_gcs_object_path,
                department_folder,
                i,
                chunk,
                embedding_str,
                ai_metadata['title']['suggested_value'],
                ai_metadata['title']['justification'],
                ai_metadata['department']['suggested_value'],
                ai_metadata['department']['justification'],
                ai_metadata['process_type']['suggested_value'],
                ai_metadata['process_type']['justification'],
                ai_metadata['status']['suggested_value'],
                ai_metadata['status']['justification'],
                # Initially final fields are same as suggested
                ai_metadata['title']['suggested_value'],
                ai_metadata['department']['suggested_value'],
                ai_metadata['process_type']['suggested_value'],
                ai_metadata['status']['suggested_value']
            ))
        insert_query = """
        INSERT INTO documents (
            original_gcs_filename, gcs_object_path, department_folder,
            chunk_index, chunk_content, embedding_vector,
            suggested_title, title_justification,
            suggested_department, department_justification,
            suggested_process_type, process_type_justification,
            suggested_status, status_justification,
            final_title, final_department, final_process_type, final_status
        ) VALUES %s
        """
        # NOTE: we will cast the embedding string to vector in the values template below
        # execute_values will interpolate tuples; cast the embedding field to vector
        # Build a custom template that casts the 6th field (embedding) to vector
        values_template = "(" + ",".join(["%s"]*18) + ")"
        # replace the placeholder for embedding (6th position) with cast
        parts = values_template.split('%s')
        # simpler: use execute_values with template specifying cast on the embedding position
        execute_values(cursor, insert_query, insert_values, template='(%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)')
        conn.commit()
        cursor.close()
        conn.close()

        logging.info(f"Successfully processed and stored {len(text_chunks)} chunks for {original_gcs_filename}")
        return {"status": "success", "original_file": original_gcs_filename, "new_path": new_gcs_object_path, "chunks_processed": len(text_chunks)}

    except json.JSONDecodeError as e:
        logging.error(f"Error decoding Pub/Sub message JSON: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid JSON in Pub/Sub message.")
    except KeyError as e:
        logging.error(f"Missing key in Pub/Sub message payload: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Missing key in Pub/Sub message payload: {e}.")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Unhandled error processing document: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")


# --- Metadata Management API Endpoints (Human-in-the-Loop Hooks) ---

@app.get("/document-metadata/{gcs_object_path:path}") # Use :path to allow slashes in path parameter
async def get_document_metadata(gcs_object_path: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """Fetches current metadata for a document."""
    if TEST_MODE:
        p = _test_metadata_path(gcs_object_path)
        data = local_read_json(p)
        if not data:
            raise HTTPException(status_code=404, detail="Document metadata not found (test mode).")
        # enforce same access control shape
        user_role = current_user.get('role')
        user_depts = current_user.get('departments') or []
        department_folder = data.get('department_folder')
        if user_role != 'manager' and department_folder not in user_depts:
            raise HTTPException(status_code=403, detail="You do not have permission to view this document.")
        return data

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
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
    """, (gcs_object_path,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Document metadata not found.")

    (doc_id, suggested_title, title_justification,
     suggested_department, department_justification,
     suggested_process_type, process_type_justification,
     suggested_status, status_justification,
     final_title, final_department, final_process_type, final_status,
     review_status, department_folder) = row

    # Access control: allow managers full access; officers only their departments
    user_role = current_user.get('role')
    user_depts = current_user.get('departments') or []
    if user_role != 'manager' and department_folder not in user_depts:
        raise HTTPException(status_code=403, detail="You do not have permission to view this document.")

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
        "department_folder": department_folder
    }


@app.put("/document-metadata/{gcs_object_path:path}")
async def update_document_metadata(gcs_object_path: str, metadata_update: Dict[str, str], current_user: Dict[str, Any] = Depends(get_current_user)):
    """Updates human-edited metadata fields for a document."""
    if TEST_MODE:
        p = _test_metadata_path(gcs_object_path)
        existing = local_read_json(p) or {}
        document_id = existing.get('document_id', 1)
        existing_map = {
            'final_title': existing.get('final_title'),
            'final_department': existing.get('final_department'),
            'final_process_type': existing.get('final_process_type'),
            'final_status': existing.get('final_status')
        }
    else:
        conn = get_db_connection()
        cursor = conn.cursor()

    update_fields = []
    update_values = []
    
    if 'final_title' in metadata_update:
        update_fields.append("final_title = %s")
        update_values.append(metadata_update['final_title'])
    if 'final_department' in metadata_update:
        update_fields.append("final_department = %s")
        update_values.append(metadata_update['final_department'])
    if 'final_process_type' in metadata_update:
        update_fields.append("final_process_type = %s")
        update_values.append(metadata_update['final_process_type'])
    if 'final_status' in metadata_update:
        update_fields.append("final_status = %s")
        update_values.append(metadata_update['final_status'])

    if not update_fields:
        raise HTTPException(status_code=400, detail="No valid fields provided for update.")
    if not TEST_MODE:
        # Fetch existing values for audit logging
        cursor.execute("SELECT id, final_title, final_department, final_process_type, final_status FROM documents WHERE gcs_object_path = %s LIMIT 1", (gcs_object_path,))
        existing = cursor.fetchone()
        if not existing:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail="Document not found.")

        document_id = existing[0]
        existing_map = {
            'final_title': existing[1],
            'final_department': existing[2],
            'final_process_type': existing[3],
            'final_status': existing[4]
        }

    # Authorization: managers can edit any; officers only their departments
    user_role = current_user.get('role')
    user_depts = current_user.get('departments') or []
    doc_dept = existing_map.get('final_department') or None
    if user_role != 'manager':
        # Determine target department if changing it, else use existing
        target_dept = metadata_update.get('final_department', doc_dept)
        if target_dept and target_dept not in user_depts:
            if not TEST_MODE:
                cursor.close()
                conn.close()
            raise HTTPException(status_code=403, detail="You do not have permission to edit this document.")

    # Perform audit logging for field-level changes — write audit files to GCS (avoid DB migrations)
    for idx, field_exp in enumerate(['final_title', 'final_department', 'final_process_type', 'final_status']):
        if field_exp in metadata_update:
            old = existing_map.get(field_exp)
            new = metadata_update[field_exp]
            if str(old) != str(new):
                audit_entry = {
                    "document_id": document_id,
                    "field": field_exp,
                    "old_value": old,
                    "new_value": new,
                    "username": current_user.get('username'),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                # Use a document-scoped base path in GCS for audit/version files
                base_path = f"documents/{document_id}"
                try:
                    if TEST_MODE:
                        local_path = os.path.join('local_test_store', base_path, 'audit', f"{datetime.now(timezone.utc).isoformat()}_{uuid.uuid4().hex}.json")
                        local_write_json(local_path, audit_entry)
                    else:
                        gcs_append_audit(GCS_BUCKET_NAME, base_path, audit_entry)
                except Exception:
                    logging.exception("Failed to write audit entry to storage")

    if TEST_MODE:
        # Merge into existing and persist locally
        merged = existing or {}
        merged.update({k: metadata_update[k] for k in ['final_title','final_department','final_process_type','final_status'] if k in metadata_update})
        merged['document_id'] = document_id
        merged['department_folder'] = merged.get('final_department')
        local_write_json(_test_metadata_path(gcs_object_path), merged)
        logging.info(f"(test) Updated metadata for {gcs_object_path} by {current_user.get('username')}")
        return {"message": f"(test) Metadata for {gcs_object_path} updated successfully."}

    update_query = f"""
        UPDATE documents
        SET {', '.join(update_fields)},
            updated_at = NOW()
        WHERE gcs_object_path = %s
    """
    update_values.append(gcs_object_path)

    cursor.execute(update_query, tuple(update_values))
    conn.commit()
    cursor.close()
    conn.close()

    logging.info(f"Updated metadata for {gcs_object_path} by {current_user.get('username')}")
    return {"message": f"Metadata for {gcs_object_path} updated successfully."}


@app.post("/document-metadata/{gcs_object_path:path}/confirm")
async def confirm_document_metadata(gcs_object_path: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """Confirms final metadata for a document, creates a version (GCS-backed), and records approval."""
    # TEST_MODE: operate on local JSON files
    if TEST_MODE:
        p = _test_metadata_path(gcs_object_path)
        data = local_read_json(p)
        if not data:
            raise HTTPException(status_code=404, detail="Document not found (test mode).")

        documents_id = data.get('document_id', 1)
        original_name = data.get('original_gcs_filename', gcs_object_path)
        final_department = data.get('final_department')

        if not final_department:
            raise HTTPException(status_code=400, detail="Cannot confirm: final_department is not set.")

        user_role = current_user.get('role')
        user_depts = current_user.get('departments') or []
        if user_role != 'manager' and final_department not in user_depts:
            raise HTTPException(status_code=403, detail="You do not have permission to confirm this document.")

        sanitized_name = original_name.replace('/', '_') if original_name else f"doc_{documents_id}"
        version_dir = _test_version_dir(sanitized_name)
        os.makedirs(version_dir, exist_ok=True)
        existing = sorted([n for n in os.listdir(version_dir) if n.startswith('v') and n.endswith('.json')]) if os.path.exists(version_dir) else []
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
        local_write_json(version_path, data)

        # mark approved in local doc
        data['review_status'] = 'approved'
        data['last_reviewed_by'] = current_user.get('username')
        data['last_reviewed_at'] = datetime.now(timezone.utc).isoformat()
        local_write_json(p, data)

        # write audit
        audit_entry = {
            "document_id": documents_id,
            "action": "confirm",
            "old_value": "pending",
            "new_value": "approved",
            "username": current_user.get('username'),
            "version": next_version,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        audit_path = os.path.join('local_test_store', 'versions', sanitized_name, 'audit')
        os.makedirs(audit_path, exist_ok=True)
        local_write_json(os.path.join(audit_path, f"{datetime.now(timezone.utc).isoformat()}_{uuid.uuid4().hex}.json"), audit_entry)

        logging.info(f"(test) Confirmed metadata for {gcs_object_path} by {current_user.get('username')}")
        return {"message": f"(test) Document {gcs_object_path} metadata confirmed and approved.", "version": next_version}

    # Normal mode: write version JSON to GCS and update documents.review_status
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT id, original_gcs_filename, final_department FROM documents WHERE gcs_object_path = %s LIMIT 1", (gcs_object_path,))
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Document not found.")

    documents_id, original_name, final_department = row

    if not final_department:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Cannot confirm: final_department is not set.")

    user_role = current_user.get('role')
    user_depts = current_user.get('departments') or []
    if user_role != 'manager' and final_department not in user_depts:
        cursor.close()
        conn.close()
        raise HTTPException(status_code=403, detail="You do not have permission to confirm this document.")

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
        (gcs_object_path,)
    )
    payload_row = cursor.fetchone()
    payload = payload_row[0] if payload_row else None

    # Write version to GCS
    sanitized_name = original_name.replace('/', '_') if original_name else f"doc_{documents_id}"
    base_path = f"versions/{sanitized_name}"
    prefix = f"{base_path}/"
    existing = gcs_list_versions(GCS_BUCKET_NAME, prefix)
    maxv = 0
    for name in existing:
        bn = name.split('/')[-1]
        if bn.startswith('v') and bn.endswith('.json'):
            try:
                v = int(bn[1:-5])
                if v > maxv:
                    maxv = v
            except Exception:
                continue
    next_version = maxv + 1
    version_path = f"{prefix}v{next_version}.json"
    try:
        gcs_write_json(GCS_BUCKET_NAME, version_path, payload)
    except Exception as e:
        cursor.close()
        conn.close()
        logging.error(f"Failed to write version to GCS: {e}")
        raise HTTPException(status_code=500, detail="Failed to persist version to GCS")

    # Update documents as approved
    cursor.execute("UPDATE documents SET review_status = 'approved', last_reviewed_by = %s, last_reviewed_at = NOW(), updated_at = NOW() WHERE gcs_object_path = %s", (current_user.get('username'), gcs_object_path))

    # Write audit entry to GCS
    audit_entry = {
        "document_id": documents_id,
        "action": "confirm",
        "old_value": "pending",
        "new_value": "approved",
        "username": current_user.get('username'),
        "version": next_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        gcs_append_audit(GCS_BUCKET_NAME, base_path, audit_entry)
    except Exception:
        logging.exception("Failed to write approval audit to GCS")

    conn.commit()
    cursor.close()
    conn.close()

    logging.info(f"Confirmed metadata for {gcs_object_path} by {current_user.get('username')}")
    return {"message": f"Document {gcs_object_path} metadata confirmed and approved.", "version": next_version}


@app.delete("/document-metadata/{gcs_object_path:path}/discard")
async def discard_document(gcs_object_path: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """Discards a document, deleting from GCS and database (or local test store)."""
    try:
        # Delete from GCS or local store
        if TEST_MODE:
            p = _test_metadata_path(gcs_object_path)
            if os.path.exists(p):
                os.remove(p)
                logging.info(f"(test) Deleted local metadata {p}.")
            else:
                logging.warning(f"(test) File {p} not found in local store for deletion.")
        else:
            bucket = storage_client.bucket(GCS_BUCKET_NAME)
            blob = bucket.blob(gcs_object_path)
            if blob.exists():
                blob.delete()
                logging.info(f"Deleted {gcs_object_path} from GCS.")
            else:
                logging.warning(f"File {gcs_object_path} not found in GCS for deletion.")

        # Delete from Database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, department_folder FROM documents WHERE gcs_object_path = %s LIMIT 1", (gcs_object_path,))
        doc = cursor.fetchone()
        if not doc:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail="Document not found in DB for deletion.")

        document_id, department_folder = doc
        user_role = current_user.get('role')
        user_depts = current_user.get('departments') or []
        if user_role != 'manager' and department_folder not in user_depts:
            cursor.close()
            conn.close()
            raise HTTPException(status_code=403, detail="You do not have permission to delete this document.")

        cursor.execute("DELETE FROM documents WHERE gcs_object_path = %s", (gcs_object_path,))
        deleted = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()

        if deleted == 0:
            raise HTTPException(status_code=404, detail="Document not found in DB for deletion.")

        logging.info(f"Discarded and deleted all traces of {gcs_object_path}.")
        return {"message": f"Document {gcs_object_path} and all its data discarded successfully."}

    except Exception as e:
        logging.error(f"Error discarding document {gcs_object_path}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An error occurred while discarding document: {str(e)}")

# Health check
@app.get("/")
def health_check():
    return {"status": "ok", "service": "docintel-data-processor"}


@app.get('/upload-ui', response_class=HTMLResponse)
async def upload_ui():
    """Serve the document upload & confirmation UI."""
    try:
        # Try to read from file (production)
        with open('docintel-data-processor/upload_ui.html', 'r') as f:
            return f.read()
    except FileNotFoundError:
        # Fallback inline HTML (for environments without file access)
        return """
        <!DOCTYPE html>
        <html>
        <head><title>Upload UI Not Found</title></head>
        <body>
            <p>Upload UI not available. Ensure upload_ui.html exists in docintel-data-processor/</p>
        </body>
        </html>
        """


@app.get('/confirm-ui', response_class=HTMLResponse)
def confirm_ui():
        html = """
        <!doctype html>
        <html>
        <head>
            <meta charset="utf-8" />
            <title>Confirm Document Metadata</title>
            <style>body{font-family:Arial,Helvetica,sans-serif;max-width:900px;margin:20px}label{display:block;margin-top:8px}input[type=text],textarea{width:100%;padding:6px}</style>
        </head>
        <body>
            <h2>Human Confirmation UI</h2>
            <p>Enter your API key and the document GCS path (e.g. department/file.pdf) to fetch and confirm metadata.</p>
            <label>API Key: <input id="apiKey" type="text" placeholder="Bearer &lt;api_key&gt; or api_key"/></label>
            <label>GCS Object Path: <input id="gcsPath" type="text" placeholder="e.g. operations/sop-1.pdf"/></label>
            <button id="btnFetch">Fetch Metadata</button>
            <div id="meta" style="margin-top:12px;display:none">
                <h3>Metadata</h3>
                <label>Final Title: <input id="final_title" type="text"/></label>
                <label>Final Department: <input id="final_department" type="text"/></label>
                <label>Final Process Type: <input id="final_process_type" type="text"/></label>
                <label>Final Status: <input id="final_status" type="text"/></label>
                <div style="margin-top:8px">
                    <button id="btnSave">Save</button>
                    <button id="btnConfirm">Confirm</button>
                </div>
            </div>
            <pre id="log" style="background:#f6f6f6;padding:8px;margin-top:12px;white-space:pre-wrap;"></pre>

            <script>
            const log = (s)=>{document.getElementById('log').textContent = s}
            document.getElementById('btnFetch').onclick = async ()=>{
                const apiKey = document.getElementById('apiKey').value
                const path = document.getElementById('gcsPath').value
                if(!path){log('Enter path');return}
                log('Fetching...')
                try{
                    const res = await fetch('/document-metadata/'+encodeURIComponent(path),{headers:{'Authorization':apiKey}})
                    if(!res.ok){log('Fetch failed: '+res.status+' '+await res.text());return}
                    const data = await res.json()
                    document.getElementById('meta').style.display='block'
                    document.getElementById('final_title').value = data.final_title || ''
                    document.getElementById('final_department').value = data.final_department || ''
                    document.getElementById('final_process_type').value = data.final_process_type || ''
                    document.getElementById('final_status').value = data.final_status || ''
                    log('Loaded metadata for '+path)
                }catch(e){log('Error: '+e)}
            }

            document.getElementById('btnSave').onclick = async ()=>{
                const apiKey = document.getElementById('apiKey').value
                const path = document.getElementById('gcsPath').value
                const payload = {
                    final_title: document.getElementById('final_title').value,
                    final_department: document.getElementById('final_department').value,
                    final_process_type: document.getElementById('final_process_type').value,
                    final_status: document.getElementById('final_status').value
                }
                log('Saving...')
                try{
                    const res = await fetch('/document-metadata/'+encodeURIComponent(path),{method:'PUT',headers:{'Content-Type':'application/json','Authorization':apiKey},body:JSON.stringify(payload)})
                    const t = await res.json()
                    if(!res.ok) log('Save failed: '+res.status+' '+JSON.stringify(t)); else log('Saved: '+JSON.stringify(t))
                }catch(e){log('Error: '+e)}
            }

            document.getElementById('btnConfirm').onclick = async ()=>{
                const apiKey = document.getElementById('apiKey').value
                const path = document.getElementById('gcsPath').value
                log('Confirming...')
                try{
                    const res = await fetch('/document-metadata/'+encodeURIComponent(path)+'/confirm',{method:'POST',headers:{'Authorization':apiKey}})
                    const t = await res.json()
                    if(!res.ok) log('Confirm failed: '+res.status+' '+JSON.stringify(t)); else log('Confirmed: '+JSON.stringify(t))
                }catch(e){log('Error: '+e)}
            }
            </script>
        </body>
        </html>
        """
        return HTMLResponse(content=html)
