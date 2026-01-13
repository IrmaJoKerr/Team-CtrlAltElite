import os
import json
import base64
import logging
from typing import List, Dict, Any, Optional
import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, HTTPException
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
async def get_document_metadata(gcs_object_path: str):
    """Fetches current metadata for a document."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            suggested_title, title_justification,
            suggested_department, department_justification,
            suggested_process_type, process_type_justification,
            suggested_status, status_justification,
            final_title, final_department, final_process_type, final_status,
            review_status
        FROM documents
        WHERE gcs_object_path = %s
        LIMIT 1
    """, (gcs_object_path,))
    metadata = cursor.fetchone()
    cursor.close()
    conn.close()

    if not metadata:
        raise HTTPException(status_code=404, detail="Document metadata not found.")

    keys = [
        "suggested_title", "title_justification",
        "suggested_department", "department_justification",
        "suggested_process_type", "process_type_justification",
        "suggested_status", "status_justification",
        "final_title", "final_department", "final_process_type", "final_status",
        "review_status"
    ]
    return dict(zip(keys, metadata))


@app.put("/document-metadata/{gcs_object_path:path}")
async def update_document_metadata(gcs_object_path: str, metadata_update: Dict[str, str]):
    """Updates human-edited metadata fields for a document."""
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

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Document not found or no changes made.")
    
    logging.info(f"Updated metadata for {gcs_object_path}")
    return {"message": f"Metadata for {gcs_object_path} updated successfully."}


@app.post("/document-metadata/{gcs_object_path:path}/confirm")
async def confirm_document_metadata(gcs_object_path: str):
    """Confirms final metadata for a document."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE documents
        SET review_status = 'approved',
            last_reviewed_by = 'human_reviewer',
            last_reviewed_at = NOW(),
            updated_at = NOW()
        WHERE gcs_object_path = %s
    """, (gcs_object_path,))
    conn.commit()
    cursor.close()
    conn.close()

    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Document not found or no changes made.")
    
    logging.info(f"Confirmed metadata for {gcs_object_path}")
    return {"message": f"Document {gcs_object_path} metadata confirmed and approved."}


@app.delete("/document-metadata/{gcs_object_path:path}/discard")
async def discard_document(gcs_object_path: str):
    """Discards a document, deleting from GCS and database."""
    try:
        # Delete from GCS
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
