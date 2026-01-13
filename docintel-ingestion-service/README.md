# DocIntel Ingestion Service

Simple FastAPI service that accepts SOP document uploads and saves them to a Google Cloud Storage bucket.

Environment
- `BUCKET_NAME` - GCS bucket to upload files to (default: `ambuckethack`).
- Google Cloud credentials: set `GOOGLE_APPLICATION_CREDENTIALS` to a service account JSON, or rely on VM/workload identity.

Run locally

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Run the app:

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8080
```

Build Docker image

```bash
docker build -t docintel-ingestion:latest .
docker run -e BUCKET_NAME=ambuckethack -e GOOGLE_APPLICATION_CREDENTIALS=/secrets/sa.json -p 8080:8080 docintel-ingestion:latest
```

Endpoint
- `POST /upload` - form file field `file` (accepts `.pdf` and `.txt`). Returns `gs://...` URI on success.
