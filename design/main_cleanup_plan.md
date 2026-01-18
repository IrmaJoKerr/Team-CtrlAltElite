# Main Cleanup Plan — docintel-data-processor

Purpose
-------
This document captures a structured, multi-pass analysis of the repository and a staged refactor plan to reduce `docintel-data-processor/main.py` to a thin orchestration layer that focuses on RAG + LLM orchestration and input/output filtering. It includes:
- an 8-pass cross-referenced inventory and analysis of the codebase
- a file-responsibility breakdown
- a 7-pass deep analysis of `docintel-data-processor/main.py` to identify extractable logic blocks
- a staged, test-driven refactor plan that preserves behavior and test coverage

Note: new service/module files and templates will be created during refactor; this document will be updated as the work proceeds.

==============================================================================
Part A — 8-Pass Cross-Referenced Analysis (repo-wide)
==============================================================================

Pass 1 — Quick surface scan (what's here)
- Top-level folders: `adapters/`, `services/`, `docintel-data-processor/`, `docintel-ingestion-service/`, `processing-service-app/`, `sop-bucket-maintenance/`, `scripts/`, `tests/`, `smoke_tests/`, `UI_store/`, `local_test_store/`, `db/`, `utils/`.
- Key production app: `docintel-data-processor/main.py` (large, ~3000 lines). UI assets live in `UI_store/`.

Pass 2 — Static inventory (see Inventory section below)
- We enumerate files and note where logic groups live (adapters, services, scripts, DB migrations, UI).

Pass 3 — Dependency & integration points
- DB: Postgres and pgvector SQL migrations in `db/`. DB access in `services/db_service.py` and many DB calls inside `main.py` and `services/*`.
- Vector store: Qdrant adapter `adapters/qdrant_adapter.py` used by `services/sync_outbox_service.py` and tests.
- Storage: local adapter `adapters/storage_adapter.py` plus `services/storage_service.py` that wraps storage calls; UI files in `UI_store/`.
- Embeddings: `services/embedding_service.py` provides `get_text_embeddings` used across code.
- Generative: `adapters/generative_adapter.py` to abstract LLM/generative model calls.

Pass 4 — Tests, smoke, and CI surfaces
- Unit tests in `tests/` (24 currently), smoke tests in `smoke_tests/`. Tests cover storage, rag, qdrant, db sync, generative adapter, and main endpoints.

Pass 5 — Security & runtime considerations
- Runtime config driven by env: MODE, TEST_MODE, DB_PASSWORD, GCS_BUCKET_NAME, EMBEDDING_MODEL_ID, etc.
- `main.py` contains auth dependency `get_current_user` and uses `DISABLE_AUTH` and `TEST_MODE` fast paths.

Pass 6 — Areas likely to be moved out of `main.py`
- Storage helpers and local-test helpers (already partially moved to `services/storage_service.py`).
- Inline HTML UI content (move to `UI_store/` — confirmed).
- DB query logic for RAG and embedding pipeline orchestration (move to `services/rag_service.py` and `services/embedding_service.py`).

Pass 7 — Operational hooks
- Background embedding worker lives in `main.py` as a placeholder; should be in `services/embedding_service.py` or a worker process.

Pass 8 — Cross-reference map (quick)
- `main.py` ↔ calls → `services/rag_service`, `services/storage_service`, `services/embedding_service`, `services/db_service`.
- `services/*` ↔ uses → `adapters/*`.

==============================================================================
Inventory (files and brief purpose)
==============================================================================

Note: new files may be created during refactor (e.g., `services/auth_service.py`, `templates/`), update this inventory after creation.

- adapters/
  - `embedding_adapter.py` — provider interface for embedding calls (local/cloud).
  - `generative_adapter.py` — provider interface for LLM/generative model calls.
  - `qdrant_adapter.py` — HTTP client to upsert/search Qdrant.
  - `secrets_adapter.py` — abstraction for secrets (env, cloud secret manager).
  - `storage_adapter.py` — local FS storage adapter with atomic move semantics.
  - `providers/` — provider-specific implementations (aws.py, google.py, registry.py).

- services/
  - `db_service.py` — connection helper, advisory locks, DB credential logic.
  - `db_sync_service.py` — logic for syncing db embeddings/outbox.
  - `embedding_service.py` — embedding batching and calls.
  - `rag_service.py` — RAG orchestration (search + snippet shaping) — partial implementation exists.
  - `storage_service.py` — higher-level storage helpers: write_json_path, gcs_write_json, list_versions, extract_text_from_pdf_gs_uri.
  - `sync_outbox_service.py` — outbox processing logic for vector db upserts.

- docintel-data-processor/
  - `main.py` — large FastAPI app: endpoints, auth dependency, UI endpoints, background worker, many helpers.
  - `requirements.txt`, `Dockerfile`.

- docintel-ingestion-service/, processing-service-app/, sop-bucket-maintenance/ — other service entrypoints each with `main.py` and Dockerfiles.

- local_test_store/ — local fixtures and test files used in TEST_MODE flows.

- UI_store/
  - `UI.html`, `upload_ui.html`, `upload_ui_new.html` — front-end HTML bundles (we will standardize on HTMX + partials here).

- db/ — SQL migrations and pgvector extension helpers.

- scripts/ — operational scripts for backfills and maintenance.

- tests/ and smoke_tests/ — unit and smoke tests.

==============================================================================
Part B — File responsibilities (detailed mapping)
==============================================================================

High-level mapping (what each file/group does):

- `adapters/*`: provider abstractions and network-level clients. Keep these small and stateless.
- `services/*`: application logic and orchestration that is independent from HTTP transport. Each service should expose a small API used by `main.py` and by tests.
  - `db_service.py`: return DB connections, handle secret resolution for DB credentials.
  - `storage_service.py`: central place for reading/writing JSON, listing versions, appending audit entries, and PDF extraction.
  - `embedding_service.py`: batching, retries, normalization of embeddings.
  - `rag_service.py`: convert user query → embedding → DB/vector search → snippet shaping and scoring; return a consistent shape for main to feed into generation.
- `docintel-data-processor/main.py`: currently mixes responsibilities; desired final responsibilities are:
  1. Hook routes to services.
  2. Input validation and authorization (FastAPI dependencies).
  3. Lightweight response shaping and filtering (sanitize LLM output, add disclaimers, audit traces) — no heavy DB or processing logic.

Other files:
- `UI_store/*`: static UI files. All inline HTML in `main.py` should be moved here as plain HTML templates and partials for HTMX.
- `scripts/*`: one-off utilities and maintenance tasks; keep them separate from application code.

==============================================================================
Part C — 7-Pass deep analysis of `docintel-data-processor/main.py`
==============================================================================

Reference: [docintel-data-processor/main.py](docintel-data-processor/main.py)

Pass 1 — Surface structure
- App initialization, CORS middleware, environment variables, auth helpers.
- Many endpoints: `/rag-query`, `/rag-query-v2`, `/process-document`, upload endpoints, metadata endpoints, embedding-status endpoints, manager workflows, UI endpoints (`/upload-ui`, `/confirm-ui`).
- Inline helper functions: `chunk_text`, `extract_text_from_pdf_gs_uri` (delegated), local TEST_MODE helpers.
- Background worker `embedding_worker_loop` is defined and scheduled on startup.

Pass 2 — Primary responsibility groups inside the file
1. HTTP route definitions and FastAPI wiring (valid role for `main.py`).
2. Inline data-processing: chunking, extraction orchestrations, embedding generation (some delegated), DB inserts for documents, and complex insert logic for document chunks.
3. Admin/manager endpoints with DB code for override resolution and precedents.
4. UI-serving endpoints and large inline HTML (now partially moved to `UI_store`).
5. Background worker scheduling and short-lived worker logic.

Pass 3 — DB & RAG logic density
- `main.py` contains SQL and DB inserts for document chunking and embedding writes, query session logging, `get_precedents` stored proc calls, and direct uses of `execute_values` for batched inserts.
- Candidate extraction: move all SQL & DB orchestration for RAG and chunk insert to `services/rag_service.py` and `services/doc_ingest_service.py` (new).

Pass 4 — LLM/generative & sanitization
- `main.py` builds prompts and calls generation via `_GENAI_AVAILABLE` or REST fallback. It sanitizes output using `sanitize_response`. Keep sanitization in a shared util (e.g., `services/content_filter.py`) and only call generator via `adapters/generative_adapter.py`.

Pass 5 — Background workers & embedding pipeline
- Worker code currently in `main.py` makes sense to move to `services/embedding_service.py` or `processing-service-app/`. Keep scheduling hooks in `main.py` minimal (e.g., on startup register background tasks that call into services).

Pass 6 — UI & static assets
- `/upload-ui` and `/confirm-ui` were serving inline HTML; moved to `UI_store`. Ensure all remaining inline HTML fragments are relocated. Endpoints should render templates or return static files.

Pass 7 — Security, config, and testability
- `main.py` mixes low-level DB error handling, GCS operations, and business logic. Best practice: services handle business logic and DB, `main.py` should handle exceptions and map to HTTP responses. Extract try/except blocks into services and have `main.py` convert exceptions to HTTPExceptions with appropriate status codes and sanitized messages.

Extractable logic blocks (pinpointed)
- Document ingestion & chunking + DB insert: extract to `services/doc_ingest_service.py`.
- Embedding orchestration (generate embeddings, normalize, return strings): `services/embedding_service.py` (already exists; extend as needed).
- RAG search and snippet shaping + confidence scoring + audit logging: `services/rag_service.py` (extend to host SQL and caching logic currently inline in `main.py`).
- Precedent retrieval & formatting: `services/precedent_service.py` or keep under `services/rag_service.py` as a helper.
- Generative prompt construction and call: keep prompt templates in `services/generative_service.py` or `adapters/generative_adapter.py` and move sanitization into `services/content_filter.py`.
- Background embedding worker: `services/worker/embedding_worker.py` or extend `services/embedding_service.py` to run as background process.
- Audit & versioning writes (GCS/local): `services/storage_service.py` (already exists) — consolidate all storage write logic there.

==============================================================================
Part D — Staged refactor plan (detailed)
==============================================================================

Goals
- Reduce `main.py` to: route wiring, auth dependency, input validation, call into services for business logic, response shaping and sanitization.

Strategy
- Small, reversible commits guarded by unit tests and smoke tests. For each extraction: (1) add delegation wrapper + new service function; (2) update callers to use service; (3) add tests to assert behavior; (4) remove original code.

Phases

Phase 0 — Prep
- Branch: `refactor/main-cleanup`.
- Run baseline tests & smoke tests; document current test status.
- Add `design/main_cleanup_plan.md` (this file) and commit.

Phase 1 — UI extraction (low-risk)
- Move any inline HTML into `UI_store/` and create minimal HTMX partials under `UI_store/partials/`.
- Update `/upload-ui` and `/confirm-ui` endpoints to serve files from `UI_store/` or render templates via `jinja2`.
- Tests: smoke tests referencing UI routes should pass.

Phase 2 — Storage consolidation
- Ensure `services/storage_service.py` exposes read/write/json helpers, list_versions, append_audit, and PDF extraction (already present). Add any missing helpers used by `main.py`.
- Update `main.py` to call `storage_service` functions; create wrappers if necessary.
- Remove old helper implementations from `main.py` after tests pass.

Phase 3 — Document ingestion extraction
- Extract the document ingestion flow from `/process-document` into `services/doc_ingest_service.py`:
  - responsibilities: get file bytes, extract text (or delegate), chunk_text, generate embeddings (via `services/embedding_service`), prepare insert values, and perform DB insert in a transaction.
  - The service returns a structured result (document_id, chunks_count, status) and raises service-specific exceptions on failures.
- `main.py` endpoint becomes a thin wrapper that validates incoming Pub/Sub payload, calls the service, and maps exceptions to HTTP responses.
- Tests: move any tests for ingestion to cover the new service; keep endpoint tests to validate HTTP translation.

Phase 4 — RAG and Query handling extraction
- Extract logical flow for `/rag-query` and `/rag-query-v2` into `services/rag_service.py` (if partially present, consolidate):
  - responsibilities: generate query embedding, perform vector search or fallback text search, shape snippets, extract procedural steps, compute confidence scores, call `services/precedent_service` as needed, return structured result.
  - Keep generation out of this service; it returns the prompt and snippets; `main.py` or a `services/generative_service` calls `adapters/generative_adapter` to create the answer.
- Tests: unit test `services/rag_service.run_rag_query` with mocked DB and embedding outputs.

Phase 5 — Generative & content filtering
- Move prompt templates and sanitization into `services/generative_service.py` and `services/content_filter.py` respectively. Use `adapters/generative_adapter.py` for actual model calls.
- `main.py` calls: `result = await rag_service.run_rag_query(...)` → `text = await generative_service.generate_answer(prompt, snippets)` → `sanitized = content_filter.sanitize_response(text)` → return.

Phase 6 — Background worker & embedding pipeline
- Move `embedding_worker_loop` into `services/embedding_service.py` (or a dedicated worker module). In `main.py` keep only lifecycle wiring (startup/shutdown) that registers background tasks when not in `TEST_MODE`.

Phase 7 — Auth/test/security polish
- Move any token/session helpers to `services/auth_service.py` if needed. Ensure `get_current_user` dependency uses auth service for token/session resolution; keep it as a thin dependency in `main.py`.

Phase 8 — Final checks, lint, CI and PR
- Run full test suite and smoke tests; run `black . && isort .`.
- Update docs and changelog, add risk/rollback notes, open PR.

Acceptance criteria
- All unit tests pass.
- Smoke tests pass.
- `main.py` file length reduced substantially; responsibilities limited to routing, auth dependency, input validation, and calling services.
- No behavior regressions for endpoints (backwards compatible unless noted and documented).

Rollback plan
- Keep each phase as a single commit; if a regression occurs, revert the commit. Use the branch for incremental PRs if a phase grows large.

==============================================================================
Part E — Todo items (first small actionable set)
==============================================================================

Initial tasks to start (copy/paste friendly):
1. `git checkout -b refactor/main-cleanup`
2. `python -m unittest discover -s tests -v` (record baseline)
3. Move inline HTML fragments from `docintel-data-processor/main.py` into `UI_store/partials/` and adjust `/upload-ui`/`/confirm-ui` to serve them (HTMX-ready partials).
4. Add service `services/doc_ingest_service.py` and implement a small wrapper that moves the ingestion logic out of `main.py` (start with stub and tests).
5. Add service `services/precedent_service.py` for `get_precedents` helper.

==============================================================================
Part F — Service DTOs, Function Signatures, and Tests Checklist (appendix)
==============================================================================

F.1 — Service DTOs & function signatures (concrete draft)
- A concrete, minimal set of Pydantic DTOs and Python service interface stubs has been drafted
  in `services/contracts.py`. These models capture the inputs/outputs we will use when
  extracting logic from `main.py` and implementing `doc_ingest_service`, `rag_service`,
  `embedding_service`, `db_service`, and `sync_outbox_service`.

- Key DTOs: `DocumentReference`, `Chunk`, `EmbeddingRequest`, `EmbeddingResponse`,
  `SearchQuery`, `SearchResult`, `OutboxItem`, `IngestResult`, `RagResponse`, `AnswerResponse`.

- Key service interfaces (abstract): `StorageServiceInterface`, `DBServiceInterface`,
  `EmbeddingServiceInterface`, `RAGServiceInterface`, `SyncOutboxServiceInterface`,
  `GenerativeServiceInterface`. Each interface documents the minimal function signatures
  (sync/async where appropriate) that `main.py` will call.

F.2 — First-phase tests checklist (Phase 1: UI + Storage)
- Unit tests:
  - **Storage:read/write**: test `services.storage_service.write_json_path` and `read_json_path` roundtrip.
  - **Storage:adapter move**: test `adapters.storage_adapter.LocalStorageAdapter.move_object` atomic and cross-device fallback.
  - **UI endpoints**: unit tests for `/upload-ui` and `/confirm-ui` to ensure they return the static shell and expected HTMX partials when requested (mock filesystem to `UI_store/`).
  - **Extracted wrapper**: test a small `ui_renderer` wrapper that `main.py` will call to serve UI files.

- Integration/smoke tests:
  - Start app in TEST_MODE and hit `/upload-ui` and `/confirm-ui`; validate returned HTML contains expected HTMX markers.
  - Run smoke tests for storage flows using `local_test_store` fixtures (already present).

F.3 — Second-phase tests checklist (Phase 2: doc_ingest_service)
- Unit tests:
  - **Chunking**: pure function tests for `chunk_text` behavior with different lengths and unicode.
  - **Embedding orchestration**: test `services.embedding_service.get_text_embeddings` with a mocked adapter to assert batching and normalization.
  - **Ingest service**: unit tests for `services.doc_ingest_service.ingest_document` covering: happy path inserts, missing file error, and partial failure rollbacks (simulate DB exceptions).
  - **Storage audit**: test `services.storage_service.append_audit` creates timestamped audit entries (mock gcs_write_json or local adapter write).

- Integration tests:
  - **End-to-end ingest** (in TEST_MODE): call the HTTP `/process-document` endpoint with a sample payload that references `local_test_store` PDF JSON; assert DB insert calls are made (mock DB) and audit entries written to local_test_store.
  - **Worker flow**: run the embedding worker loop for a short run with test outbox rows and assert the expected Qdrant upsert calls were triggered (mock network adapter).

F.4 — Where these artifacts live
- DTO and interface stubs: `services/contracts.py` (Python) — used as the canonical contract during refactor.
- Tests checklist will be added next to the relevant services as `tests/test_<service>_contracts.py` stubs and then implemented per-phase.

==============================================================================
End of append

Appendix — Notes & Recommendations
==============================================================================
- Use HTMX for frontend: server-rendered partials and an orchestrator page (put `UI_store/UI.html` as the main shell). HTMX reduces client-side complexity and keeps auth and session handling on the backend.
- Keep tests small and cover the new services as you move code.
- Update this document as you make changes; include file diffs in PR description.

---
Generated on: 2026-01-18
