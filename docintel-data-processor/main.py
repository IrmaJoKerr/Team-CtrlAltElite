import os
import json
import base64
import logging
from typing import List, Dict, Any, Optional
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import uuid

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import HTMLResponse
from google.cloud import storage, pubsub_v1, secretmanager
from vertexai.preview.generative_models import GenerativeModel, Part
from vertexai.language_models import TextEmbeddingModel
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

# Vertex AI Models
embedding_model = TextEmbeddingModel.from_pretrained("text-embedding-004")
generative_model = GenerativeModel("gemini-pro")

# Environment Variables
PROJECT_ID = os.environ.get('PROJECT_ID', 'ctrlaltelite-484111')
REGION = os.environ.get('REGION', 'us-central1')
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
    """Generates embeddings for a list of texts using Vertex AI."""
    try:
        embeddings = []
        for i in range(0, len(texts), 250):
            batch_texts = texts[i:i+250]
            logging.info(f"Generating embeddings for batch {i/250 + 1}...")
            response = await embedding_model.get_embeddings_async(batch_texts)
            embeddings.extend([embedding.values for embedding in response.embeddings])
        return embeddings
    except Exception as e:
        logging.error(f"Failed to get text embeddings from Vertex AI: {e}", exc_info=True)
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
    loop = asyncio.get_running_loop()
    try:
        logging.info("Requesting AI metadata suggestions from Gemini-Pro (in thread)...")

        def call_generate():
            # Blocking call - run in threadpool
            return generative_model.generate_content(prompt)

        response = await loop.run_in_executor(None, call_generate)

        # Robust parsing: validate path existence before accessing
        text_response = None
        try:
            cand = getattr(response, 'candidates', None)
            if cand and len(cand) > 0:
                content = getattr(cand[0], 'content', None)
                if content and getattr(content, 'parts', None) and len(content.parts) > 0:
                    part = content.parts[0]
                    text_response = getattr(part, 'text', None)

        except Exception:
            text_response = None

        if not text_response:
            raise ValueError('Unexpected Vertex response structure')

        # strip markdown fences if present
        if text_response.startswith("```json"):
            # remove leading fence and optional trailing fence
            try:
                # find closing fence
                end = text_response.rfind("```")
                if end != -1:
                    text_response = text_response[7:end]
                else:
                    text_response = text_response[7:]
            except Exception:
                text_response = text_response[7:]

        parsed = json.loads(text_response)
        return parsed
    except Exception as e:
        logging.error(f"Failed to get AI metadata suggestions: {e}", exc_info=True)
        return {
            "title": {"suggested_value": None, "justification": "AI extraction failed.", "confidence_score": 0.0},
            "department": {"suggested_value": None, "justification": "AI extraction failed.", "confidence_score": 0.0},
            "process_type": {"suggested_value": None, "justification": "AI extraction failed.", "confidence_score": 0.0},
            "status": {"suggested_value": None, "justification": "AI extraction failed.", "confidence_score": 0.0}
        }

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
        ai_metadata = await get_ai_metadata_suggestions(document_text)
        
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
