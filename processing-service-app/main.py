import os
import json
import functions_framework
import logging
from google.cloud import storage, secretmanager
from google.cloud.sql.connector import Connector
try:
    # Newer versions expose the RAG client here
    from google.cloud.aiplatform import RagCorpusServiceClient, ImportRagFilesRequest
except Exception:
    try:
        # Older/newer packaging may expose the v1 client module
        from google.cloud.aiplatform_v1 import RagCorpusServiceClient, ImportRagFilesRequest
    except Exception:
        RagCorpusServiceClient = None
        ImportRagFilesRequest = None
import pg8000.dbapi # Required for Connector to work with pg8000
import uuid # For generating UUIDs for SOPs if needed

# Initialize Google Cloud clients
storage_client = storage.Client()
secret_client = secretmanager.SecretManagerServiceClient()

# --- Configuration from Environment Variables (set by Terraform) ---
PROJECT_ID = os.environ.get("PROJECT_ID")
REGION = os.environ.get("REGION") # Added REGION env var
DB_INSTANCE_CONNECTION_NAME = os.environ.get("DB_INSTANCE_CONNECTION_NAME")
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_SECRET_NAME = os.environ.get("DB_SECRET_NAME")
GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
RAG_CORPUS_NAME = os.environ.get("RAG_CORPUS_NAME") # Full resource name, e.g., projects/PROJECT_ID/locations/REGION/ragCorpora/CORPUS_ID

logging.basicConfig(level=logging.INFO)

def get_db_password():
    """Retrieves the database password from Secret Manager."""
    secret_resource_name = f"projects/{PROJECT_ID}/secrets/{DB_SECRET_NAME}/versions/latest"
    try:
        response = secret_client.access_secret_version(request={"name": secret_resource_name})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        logging.error(f"Failed to retrieve DB password from Secret Manager: {e}")
        raise

def get_connection():
    """Establishes a secure connection to Cloud SQL."""
    db_pass = get_db_password()
    logging.info(f"Connecting to Cloud SQL instance: {DB_INSTANCE_CONNECTION_NAME}")
    try:
        conn = Connector().connect(
            DB_INSTANCE_CONNECTION_NAME,
            "pg8000",
            user=DB_USER,
            password=db_pass,
            db=DB_NAME,
        )
        return conn
    except Exception as e:
        logging.error(f"Failed to connect to Cloud SQL: {e}")
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
        # 1. Download document from GCS (if needed for extraction)
        # For simplicity, we'll just get content if we need to parse it here.
        # Vertex AI RAG Engine can read directly from GCS.
        blob = storage_client.bucket(bucket_name).blob(file_name)
        file_content = blob.download_as_bytes()

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

        # 4. Add document to Vertex AI RAG Corpus
        # Vertex AI RAG Engine simplifies this: you tell it the GCS URI
        # It handles chunking, embedding, and indexing.
        # The 'state' in Cloud SQL is 'Draft' at this point.
        # In a real system, RAG indexing would happen when a version becomes 'Active'.
        # For hackathon MVP, we'll index all ingested documents directly to simplify demo.

        if RagCorpusServiceClient is None or ImportRagFilesRequest is None:
            logging.warning("RAG import client not available in this runtime; skipping RAG import.")
        else:
            rag_corpus_service_client = RagCorpusServiceClient(
                client_options={"api_endpoint": f"{REGION}-aiplatform.googleapis.com"}
            )

            request = ImportRagFilesRequest(
                parent=RAG_CORPUS_NAME,
                import_rag_files_config=ImportRagFilesRequest.ImportRagFilesConfig(
                    gcs_source=ImportRagFilesRequest.ImportRagFilesConfig.GcsSource(
                        uris=[gcs_uri]
                    ),
                    rag_file_chunking_config=ImportRagFilesRequest.ImportRagFilesConfig.RagFileChunkingConfig(
                        chunk_size=512 # You can adjust chunk size
                    )
                )
            )

            logging.info(f"Importing {gcs_uri} to RAG Corpus {RAG_CORPUS_NAME}...")
            operation = rag_corpus_service_client.import_rag_files(request=request)
            # The import operation is asynchronous, so we don't wait for completion here for MVP.
            # In production, you'd monitor this operation.
            try:
                op_name = getattr(operation, "operation", None)
                if op_name is None:
                    logging.info(f"Started RAG file import operation: {operation}")
                else:
                    logging.info(f"Started RAG file import operation: {op_name.name}")
            except Exception:
                logging.info("Started RAG file import operation (could not parse operation name)")
            logging.info(f"Successfully processed {file_name}.")

    except Exception as e:
        logging.error(f"Error processing file {file_name}: {e}", exc_info=True)
        if conn:
            conn.rollback() # Rollback DB transaction on error
        # Re-raise the exception to indicate failure to Cloud Run/Pub/Sub
        raise

    finally:
        if conn:
            conn.close()

