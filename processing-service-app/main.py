import os
import json
import functions_framework
import logging
# Local-first: remove Google Cloud SDK dependencies. Use adapters for storage/indexing.
import uuid # For generating UUIDs for SOPs if needed
import psycopg2

# --- Configuration from Environment Variables ---
REGION = os.environ.get("REGION")
DB_HOST = os.environ.get("DB_HOST")
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
# prefer DB_PASSWORD from env; if absent and running in cloud mode, secrets adapter will be used
DB_PASSWORD = os.environ.get("DB_PASSWORD")
STORAGE_ROOT = os.environ.get('STORAGE_ROOT')

# Runtime mode: optional but if provided must be 'local' or 'cloud'
MODE = os.environ.get('MODE')
if MODE is not None and MODE not in ('local', 'cloud'):
    raise RuntimeError('Invalid MODE. Set MODE=local or MODE=cloud')
CLOUD_MODE = True if MODE == 'cloud' else False

logging.basicConfig(level=logging.INFO)

def get_connection():
    """Establishes and returns a PostgreSQL database connection using psycopg2.

    Requires `DB_PASSWORD` to be set in the environment for local-first operation.
    """
    pw = DB_PASSWORD
    if not pw:
        if CLOUD_MODE:
            try:
                from adapters.secrets_adapter import get_db_password
                pw = get_db_password(cloud_mode=True, config=None)
            except Exception as e:
                logging.error('Failed to obtain DB password from secrets adapter: %s', e)
                raise RuntimeError('Cloud mode selected but DB password unavailable. Set --secret-provider or SECRET_PROVIDER env var, or set DB_PASSWORD env.')
        else:
            logging.error('DB_PASSWORD not set; cannot connect to database')
            raise RuntimeError('DB_PASSWORD not set')

    try:
        conn = psycopg2.connect(host=DB_HOST, user=DB_USER, password=pw, dbname=DB_NAME)
        return conn
    except Exception as e:
        logging.error(f"Failed to connect to database: {e}", exc_info=True)
        raise

# --- Placeholder for Document Parsing and Metadata Extraction ---
def extract_sop_metadata(file_content_bytes, file_name):
    """
    Extracts metadata from the SOP document.
    In a real scenario, this would involve NLP, regex, or Document AI.
    For Hackathon MVP, we'll use simple placeholders based on filename.
    """
    logging.info(f"Extracting metadata for {file_name}...")
    
    department = "General"
    sensitivity_level = "Internal"
    document_type = "SOP"
    effective_date = "2023-01-01" # Placeholder, ideally parsed from content or filename
    sop_owner = "Admin"

    # Simple logic for hackathon demo
    if "HR" in file_name.upper():
        department = "Human Resources"
        sensitivity_level = "Confidential"
    elif "FINANCE" in file_name.upper():
        department = "Finance"
        sensitivity_level = "Strictly Confidential"
    elif "LEGAL" in file_name.upper():
        department = "Legal"
        document_type = "Policy"

    metadata = {
        "department": department,
        "sensitivity_level": sensitivity_level,
        "document_type": document_type,
        "effective_date": effective_date,
        "sop_owner": sop_owner,
        "original_filename": file_name # Keep original filename for reference
    }
    logging.info(f"Extracted metadata: {metadata}")
    return metadata

# --- Pub/Sub Triggered Cloud Run Function ---
@functions_framework.cloud_event
def process_sop_document(cloud_event):
    """
    Triggered by a Cloud Storage event (new object created).
    Processes the SOP document: extracts metadata, stores in DB, and adds to RAG Corpus.
    """
    data = cloud_event.data
    bucket_name = data["bucket"]
    file_name = data["name"]
    gcs_uri = f"gs://{bucket_name}/{file_name}"

    logging.info(f"Received GCS event for file: {gcs_uri}")

    conn = None # Initialize conn to None
    try:
        # 1. Download document from storage (local adapter preferred, fallback to GCS)
        file_content = None
        try:
            from adapters.storage_adapter import get_storage_adapter
            adapter = get_storage_adapter()
            obj_name = f"{bucket_name}/{file_name}".lstrip('/')
            file_content = adapter.read_bytes(obj_name)
        except Exception:
            if storage_client:
                blob = storage_client.bucket(bucket_name).blob(file_name)
                file_content = blob.download_as_bytes()
            else:
                raise

        # 2. Extract Metadata
        sop_metadata = extract_sop_metadata(file_content, file_name)

        # 3. Store Metadata in Cloud SQL
        conn = get_connection()
        cursor = conn.cursor()

        # For MVP, let's assume each file is a new SOP.
        # In a real system, you'd check if an SOP with this logical identifier already exists
        # and either create a new SOP or a new version for an existing SOP.
        # For hackathon, we'll create a new SOP and its first version as 'Draft'.
        
        # Insert into sops table
        cursor.execute(
            "INSERT INTO sops (department, sop_owner) VALUES (%s, %s) RETURNING sop_id",
            (sop_metadata["department"], sop_metadata["sop_owner"])
        )
        sop_id = cursor.fetchone()[0]
        logging.info(f"Created new SOP with ID: {sop_id}")

        # Insert into sop_versions table
        cursor.execute(
            """
            INSERT INTO sop_versions (sop_id, state, gcs_path, metadata, editor_identity, change_reason)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING version_id
            """,
            (sop_id, 'Draft', gcs_uri, json.dumps(sop_metadata), 'auto-ingest-service', 'Initial ingestion via GCS upload')
        )
        version_id = cursor.fetchone()[0]
        logging.info(f"Created new SOP version {version_id} for SOP {sop_id} as 'Draft'.")

        # Log ingestion action
        cursor.execute(
            """
            INSERT INTO audit_log (actor_identity, action, sop_id, to_version_id, justification)
            VALUES (%s, %s, %s, %s, %s)
            """,
            ('processing-service', 'INGEST', sop_id, version_id, f'Document {file_name} ingested.')
        )
        conn.commit()
        logging.info("Metadata and audit log saved to Cloud SQL.")

        # 4. Indexing step: use a pluggable adapter for indexing/vector store.
        # The original code used a hosted RAG service; this repo uses adapters so
        # you can index to a local vector store (Qdrant) or any hosted index.
        try:
            from adapters.index_adapter import index_document_if_available
            index_document_if_available(sop_id, version_id, file_content, metadata=sop_metadata)
            logging.info(f"Indexing requested for {file_name} via adapter")
        except Exception:
            logging.info("No index adapter available; skipping indexing step")

    except Exception as e:
        logging.error(f"Error processing file {file_name}: {e}", exc_info=True)
        if conn:
            conn.rollback() # Rollback DB transaction on error
        # Re-raise the exception to indicate failure to Cloud Run/Pub/Sub
        raise

    finally:
        if conn:
            conn.close()

