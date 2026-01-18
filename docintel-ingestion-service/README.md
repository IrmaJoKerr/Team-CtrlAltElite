# DocIntel Ingestion Service

Simple FastAPI service that accepts SOP document uploads and saves them using the configured storage adapter (local filesystem by default).

Environment
- `BUCKET_NAME` / `STORAGE_ROOT` - storage prefix or local root for uploaded files.
- The service prefers `DB_PASSWORD` and `STORAGE_ROOT` for local-first development. If you need a hosted provider, configure a storage adapter and provider credentials as required.

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
docker run -e STORAGE_ROOT=/data/uploads -p 8080:8080 docintel-ingestion:latest
```

Endpoint
- `POST /upload` - form file field `file` (accepts `.pdf` and `.txt`). Returns storage path on success.
