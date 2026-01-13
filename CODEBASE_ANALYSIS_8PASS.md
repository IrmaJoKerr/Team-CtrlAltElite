# 8-Pass Codebase Architecture Analysis
**Status**: Complete  
**Date**: Current Session  
**Context**: Preparing to implement embedding pipelines with document-type differentiation (immediate vs 2-hour delayed)

---

## PASS 1: Architecture Overview & Service Topology

### Services Identified
| Service | File | Purpose | Status |
|---------|------|---------|--------|
| **Data Processor** | docintel-data-processor/main.py | Central FastAPI backend; handles document processing, RAG queries, embeddings, precedent matching, manager workflows | 3100 lines, Complete, No errors |
| **Ingestion Service** | docintel-ingestion-service/main.py | Lightweight GCS uploader; accepts PDF/TXT files | ~50 lines, Simple |
| **Processing App** | processing-service-app/main.py | Cloud Functions handler; metadata extraction, RAG corpus ingestion | 215 lines, Placeholder logic |
| **Bucket Maintenance** | sop-bucket-maintenance/main.py | Housekeeping for GCS bucket | To be analyzed |

### Cloud Architecture
```
Cloud Storage (us-central1)
    ↓ [Pub/Sub event]
Cloud Run: docintel-data-processor (us-central1)
    ├─→ Vertex AI: embedding-004 (us-central1)
    ├─→ Cloud SQL (us-west1)
    └─→ Amcorpus RAG (us-west1)
```

### Regions & Latency
- **Cloud Run**: us-central1 (main service)
- **Cloud SQL**: us-west1 (database)
- **Amcorpus**: us-west1 (vector corpus)
- **Estimated latency**: 80-100ms between us-central1 and us-west1 (acceptable for MVP)

---

## PASS 2: Document Processing Pipeline

### Current Flow
```
1. User uploads PDF/TXT via ingestion-service
   └─→ GCS: gs://ambuckethack/{original_filename}

2. Pub/Sub event triggered
   └─→ process_document_from_pubsub() in main.py

3. Document Processing (lines 2450-2610):
   ├─→ Download blob from GCS (original root location)
   ├─→ Extract text (PDF via pypdf, TXT/MD via download_as_text)
   ├─→ Call get_ai_metadata_suggestions() for classification
   │   └─→ Vertex AI gemini-2.5-pro (timeout: 25 seconds)
   ├─→ Move GCS object: root/{filename} → {department}/{filename}
   │   └─→ Copy → Delete (for atomicity)
   ├─→ Chunk text (assuming split_text_into_chunks() function exists)
   └─→ Generate embeddings for each chunk
       └─→ get_text_embeddings() via Vertex API (batch processing)

4. Database Insert (lines 2540-2610):
   ├─→ Prepare pgvector-compatible embedding strings
   ├─→ INSERT INTO documents with 18 columns:
   │   - Chunk metadata: original_gcs_filename, gcs_object_path, department_folder, chunk_index, chunk_content
   │   - Embedding: embedding_vector (vector type, cast via ::vector)
   │   - AI suggestions: suggested_title, title_justification, suggested_department, ...
   │   - Final values: final_title, final_department, final_process_type, final_status
   └─→ COMMIT (atomic batch insert)

5. Return Success Response
   └─→ {"status": "success", "chunks_processed": N, "original_file": "..."}
```

### Key Observations
- **No async background job**: Embeddings generated synchronously in Pub/Sub handler
- **No status tracking**: No `embedding_status` field in documents table
- **No retry logic**: Single attempt; if fails, document is in bad state
- **No document_type field**: All documents treated uniformly
- **Single-speed processing**: No 2-hour delay for SOPs
- **Data inserted immediately**: Embeddings stored in pgvector before user can confirm quality

### Issues Identified
1. **Race condition risk**: Query endpoints (get_precedents) can be called before embeddings are complete
2. **No failure recovery**: If embedding API times out, document chunks created but embeddings NULL
3. **SOP quality loss**: SOP documents embedded immediately without edit window

---

## PASS 3: Database Schema Inspection

### Documents Table Structure (inferred from INSERT at lines 2567-2578)
```sql
CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,                      -- Implicit
    original_gcs_filename TEXT UNIQUE,
    gcs_object_path TEXT,
    department_folder TEXT,
    chunk_index INT,
    chunk_content TEXT,
    embedding_vector vector(1536),              -- Added via migrate_add_embedding_vector.sql
    suggested_title TEXT,
    title_justification TEXT,
    suggested_department TEXT,
    department_justification TEXT,
    suggested_process_type TEXT,
    process_type_justification TEXT,
    suggested_status TEXT,
    status_justification TEXT,
    final_title TEXT,
    final_department TEXT,
    final_process_type TEXT,
    final_status TEXT
    -- MISSING: document_type, embedding_status, embedding_eligible_at, embedding_attempt_count
);

-- Existing indexes (from migrate_add_embedding_vector.sql):
CREATE INDEX documents_embedding_vector_idx 
    ON documents USING ivfflat (embedding_vector vector_cosine_ops);
```

### Related Tables
- **override_log** (migrate_precedent_system.sql): Override justifications, audit trail
  - Columns: override_id, user_id, is_resolved, resolution_notes, resolution_date, resolved_by_user_id
  - **MISSING**: document_type, embedding_status fields
  
- **sop_documents** (migrate_add_versioning_and_audit.sql): SOP metadata
  - Columns: id, original_gcs_filename, created_at
  - **Note**: Separate table; not linked to documents chunks
  
- **sop_versions**: Version history of SOPs
  - Columns: id, document_id, version, payload, created_by, created_at
  
- **upload_metadata_drafts**: User-edited metadata before confirmation
  - Designed for editing window; not currently integrated with embedding timing

### Migration Files Inventory
| File | Purpose | Lines | Status |
|------|---------|-------|--------|
| create_pgvector_extension.sql | Enable pgvector extension | - | Applied |
| migrate_add_embedding_vector.sql | Add embedding_vector column | 16 | Applied |
| migrate_add_versioning_and_audit.sql | Create versioning tables | 50+ | Applied |
| migrate_precedent_system.sql | Precedent system (pg_trgm) | 178 | Applied |
| migrate_upload_sessions.sql | Upload session tracking | 85+ | Applied |
| migrate_query_audit.sql | Query audit trail | - | Not analyzed |
| migrate_sop_version_status.sql | SOP status fields | - | Not analyzed |

---

## PASS 4: Embedding Generation Function

### get_text_embeddings() Implementation (lines 304-430)

**Location**: docintel-data-processor/main.py, lines 304-430

**Signature**: `async def get_text_embeddings(texts: List[str]) -> List[List[float]]`

**Processing Strategy**:
1. **Batch Processing**: Splits texts into batches (batch_size not shown, need to verify)
2. **Dual API Support**:
   - Primary: google-genai SDK (EMBEDDING_MODEL_ID = "text-embedding-004")
   - Fallback: REST endpoint to custom Vertex AI endpoint (EMBEDDING_ENDPOINT)
3. **Error Handling**: Catches API errors, logs them, no retry logic within function
4. **Timeout**: Inherited from caller's timeout (25 seconds in process_document context)

**Key Code Path**:
```python
# Line 335: Primary path - GenAI SDK
resp = _GENAI_CLIENT.models.embed_content(model=EMBEDDING_MODEL_ID, contents=txt)

# Extract embedding from response (lines 338-343):
if hasattr(resp, 'embedding'):
    emb = getattr(resp, 'embedding')
elif hasattr(resp, 'embeddings'):
    emb = resp.embeddings[0]
elif hasattr(resp, 'data') and len(resp.data) > 0 and hasattr(resp.data[0], 'embedding'):
    emb = resp.data[0].embedding

# Fallback path (line 363):
# REST call to EMBEDDING_ENDPOINT if SDK fails
```

**Issues**:
- No retry logic (single attempt)
- No timeout handling (relies on caller)
- No status tracking
- Returns embeddings without success/failure metadata
- Assumes successful response formatting (brittle)

---

## PASS 5: Pub/Sub Orchestration & Async Patterns

### Pub/Sub Message Flow
**Endpoint**: `POST /process-document` (lines 2450+)
**Trigger**: Cloud Storage Pub/Sub event on file upload
**Payload**: Cloud Storage Notification format
```json
{
  "message": {
    "data": "<base64-encoded GCS event JSON>"
  }
}
```

**Current Async Pattern**:
```python
@app.post("/process-document")
async def process_document_from_pubsub(request: Request):
    # 1. Decode Pub/Sub message (sync)
    # 2. Download blob (sync, blocking I/O)
    # 3. Extract text (sync, CPU-bound)
    # 4. AI metadata (async call, but awaited inline)
    # 5. Move GCS object (sync, blocking I/O)
    # 6. Chunk text (sync, CPU-bound)
    # 7. Generate embeddings (async, awaited inline) ← BOTTLENECK
    # 8. Insert to DB (sync, blocking I/O)
    # 9. Return response
```

**Critical Issue**: Step 7 (embeddings) awaited synchronously in HTTP handler
- If embedding generation takes 15 seconds, HTTP response delayed
- Cloud Run timeout: 60 minutes default, but bad UX
- No background job queue (no Bull, Celery, Cloud Tasks)
- All processing happens in request/response cycle

### App Initialization (lines ~100-200)
- Lazy-loaded models: _GENAI_CLIENT, _EMBEDDING_MODEL, _GENERATIVE_MODEL
- Thread-safe auth session: _AUTH_SESSION with lock
- No background workers initialized on startup

**Finding**: No async background job pattern exists in current codebase. All work is request-driven.

---

## PASS 6: Manager Workflows & Override Processing

### Override Creation (implicit, not fully visible)
**Current status**: Unclear where override_log records are initially created. Searching for INSERT shows queries on lines 2183, 2211, 2274, 2375, 2384, 2391 (all SELECT, not INSERT).

**Hypothesis**: 
- Override created via loan officer UI → /upload-document or similar endpoint (not analyzed yet)
- Override justification stored → Triggers Pub/Sub → document processed → embeddings generated
- Override_log record created with: override_id, user_id, justification, created_at

### Manager Approval Flow (lines ~2170-2300)
**Endpoint**: `POST /override/{id}/resolve` (manager/compliance-only)
```python
@manager_or_compliance_only
async def resolve_override(override_id: UUID, ...):
    # 1. Check if override exists & not yet resolved
    # 2. Validate user has permission
    # 3. Update override_log: is_resolved=TRUE, resolution_notes=SOP, resolution_date=now()
    # 4. Trigger SOP embedding refresh (not shown, may be implicit)
    # 5. Return updated override with audit trail
```

**Fields Updated**:
- is_resolved: TRUE
- resolution_notes: Manager-edited SOP justification
- resolution_date: NOW()
- resolved_by_user_id: current_user_id

### Issue
- No `document_type` field in override_log to differentiate embedding timing
- SOP updates (resolution_notes) not tracked separately for embedding eligibility

---

## PASS 7: Query & Precedent Matching System

### get_precedents() Function (lines ~848-970, called from /rag-query-v2)

**Purpose**: Find similar past override cases
**Mechanism**: Fuzzy text matching + stored function call

**Stored Function**: get_similar_precedents() in PostgreSQL (migrate_precedent_system.sql)
```sql
-- Fuzzy matching using pg_trgm extension
SELECT override_id, justification, similarity_score
FROM override_log
WHERE similarity(justification, %s) >= 0.7  -- 70% threshold
ORDER BY similarity DESC
LIMIT 5
```

**Blocking Condition** (CRITICAL for embedding status):
```python
# Line ~900 (inferred from description):
if document.embedding_status != 'complete':
    return PrecedentResponse(precedents=[], warning="Embeddings not ready yet")
```

**Current Issue**:
- No embedding_status field exists, so this check cannot be implemented
- Precedent matching can execute against NULL embeddings → wrong/no results

### Vector Search Integration
**Endpoint**: `/rag-query-v2` (lines ~1540-1580)
```python
# pgvector nearest neighbor search (line 684):
SELECT chunk_content, gcs_object_path 
FROM documents 
WHERE embedding_vector IS NOT NULL
ORDER BY embedding_vector <-> %s::vector LIMIT 5
```

**Conditions for Success**:
1. Documents table must have non-NULL embedding_vector values
2. embedding_vector must be a vector type (1536 dimensions for text-embedding-004)
3. Index must be built: documents_embedding_vector_idx (ivfflat)

---

## PASS 8: Integration Points & Dependencies

### Data Flow Dependencies
```
Pub/Sub Upload Event
    └─→ process_document_from_pubsub()
        ├─→ get_text_embeddings()               [NO STATUS TRACKING]
        ├─→ INSERT INTO documents               [IMMEDIATE, NO DELAY]
        └─→ Return 200 OK
            
User Queries Document
    └─→ /rag-query-v2
        ├─→ Vector search (SELECT WHERE embedding_vector IS NOT NULL)
        └─→ get_precedents()                    [NEEDS embedding_status CHECK]
```

### Missing Components for Embedding Pipeline
1. **embedding_status Tracking**
   - Required columns: `embedding_status` ('pending' | 'complete' | 'failed')
   - Required columns: `embedding_eligible_at` (timestamp for 2-hour delay)
   - Required columns: `embedding_attempt_count` (retry tracking)
   - Required columns: `document_type` ('sop' | 'override')

2. **Background Job System**
   - No existing background job queue (Celery, Bull, RabbitMQ)
   - Could be implemented as:
     a) Async loop in main.py (simple, good for MVP)
     b) Separate Cloud Run service with manual retry
     c) Cloud Tasks queue (enterprise approach)
   - **Recommendation**: Option (a) - async loop in main.py, started on app startup

3. **Retry Logic**
   - Current: Single attempt in get_text_embeddings()
   - Needed: Exponential backoff with max 1 retry
   - Storage: embedding_attempt_count field

4. **Admin Notifications**
   - On final embedding failure, notify admin
   - Could use:
     - Pub/Sub topic for notifications
     - Email service (SendGrid, Mailgun)
     - Slack webhook
   - **Not yet integrated**, placeholder behavior exists

5. **UI Status Polling**
   - Needed endpoint: `GET /documents/{id}/embedding-status`
   - Returns: `{"status": "pending|complete|failed", "eta": "10min"}`
   - UI displays static banner: "Embedding in progress... (estimated 10 min)"

### Amcorpus Integration Point
**Location**: Not fully visible in current analysis, but referenced in RAG queries

**Process** (inferred):
1. After document embeddings complete in pgvector
2. Embeddings must be ingested into Amcorpus RAG corpus
3. Used for Gemini's RAG retrieval in /rag-query-v2

**Missing**: Explicit code showing Amcorpus ingestion; may be in processing-service-app/main.py (not fully analyzed)

---

## SUMMARY TABLE: Current State vs. Required State

| Component | Current | Required | Impact |
|-----------|---------|----------|--------|
| `embedding_status` column | ❌ Missing | ✅ Required | Blocks precedent queries, race conditions |
| `document_type` column | ❌ Missing | ✅ Required | Can't differentiate SOP vs override timing |
| `embedding_eligible_at` column | ❌ Missing | ✅ Required | Can't enforce 2-hour SOP delay |
| `embedding_attempt_count` column | ❌ Missing | ✅ Required | Can't track retries |
| Async embedding loop | ❌ Missing | ✅ Required | Embeddings currently sync (slow) |
| Retry logic in embeddings | ❌ Missing | ✅ Required | No failure recovery |
| Status check endpoint | ❌ Missing | ✅ Required | UI can't poll progress |
| get_precedents() guard | ❌ Not implemented | ✅ Required | Wrong precedents if embeddings incomplete |
| Admin notification on failure | ❌ Missing | ✅ Required | Silent failures (data loss risk) |

---

## IMPLEMENTATION ROADMAP

### Phase 1: Database Schema (1 hour)
1. Create `db/migrate_embedding_pipelines.sql`
2. Add 4 columns to documents table
3. Add index on (document_type, embedding_status)
4. Add check constraint on embedding_status values
5. Apply migration via Cloud SQL client

### Phase 2: Background Embedding Job (2 hours)
1. Create `async def embedding_worker_loop()` in main.py
2. Poll documents WHERE embedding_status = 'pending' every 60s
3. For each document:
   - Check `embedding_eligible_at`: if SOP and NOW() < eligible_at, skip
   - Call get_text_embeddings() with retry logic
   - On success: UPDATE embedding_status = 'complete'
   - On failure (retry exhausted): UPDATE embedding_status = 'failed' + notify admin
4. Start worker on app startup with: `asyncio.create_task(embedding_worker_loop())`

### Phase 3: Status Endpoints (45 min)
1. `GET /documents/{id}/embedding-status` → returns status + ETA
2. `GET /documents/embedding-status/bulk` → batch status check (for UI dashboard)

### Phase 4: Manager Workflows Update (30 min)
1. When manager resolves override (POST /override/{id}/resolve):
   - Insert/update resolution justification as new document chunk
   - Mark document_type = 'override'
   - Trigger embedding with immediate timing

### Phase 5: UI Updates (30 min)
1. Add embedding status banner in Upload section
2. Add status polling to Upload section
3. Disable Query section until embedding_status = 'complete'

### Phase 6: Integration Tests (4 hours - after implementation)
1. Test database migrations
2. Test embedding status lifecycle (pending → complete → used in query)
3. Test retry logic (simulate Vertex timeout)
4. Test precedent matching after embedding completes
5. Test SOP delay (mark eligible_at in future, verify not embedded early)

---

## CRITICAL FINDINGS

### 🚨 High Priority
1. **Race Condition**: get_precedents() can execute before embeddings complete
   - **Fix**: Check embedding_status = 'complete' in get_precedents()
   
2. **Data Loss Risk**: Failed embeddings marked NULL with no recovery
   - **Fix**: Retry logic + failed status tracking
   
3. **SOP Quality**: SOP documents embedded immediately, no edit window
   - **Fix**: Add embedding_eligible_at delay for SOPs

### ⚠️ Medium Priority
1. **Performance**: Embedding generation blocks HTTP handler (25s timeout)
   - **Fix**: Move to async background job
   
2. **Observability**: No way to track embedding progress
   - **Fix**: Status endpoint + UI banner

### ℹ️ Low Priority
1. **Code Structure**: main.py is monolithic (3100 lines)
   - **Decision**: No refactoring per user requirements
   
2. **Documentation**: No API schema documentation
   - **Decision**: MVP priority, docs later

---

## NEXT STEPS

✅ 8-pass analysis complete. Proceed to:
1. Create embedding_pipelines migration
2. Implement background worker + retry logic
3. Add status endpoints
4. Update UI
5. Run integration tests

