Smoke tests (local-first)
=========================

This folder contains lightweight, mocked smoke tests for the project. They are
intended to run locally without real cloud credentials or endpoints.

Files
- `smoke_test_embedding_and_gen.py` — Mocked embedding + generative smoke test. Uses an in-process
  mock session and a deterministic fake embedding adapter so it runs reliably.
- `stress_endpoints.py` — TestClient-based stress test that monkeypatches storage, DB and network
  helpers for local execution.
- `run_all_tests.py` — Small runner that executes the smoke tests and collects logs.

Running

To run the mocked smoke tests (recommended):

```bash
python3 smoke_tests/run_all_tests.py
```

To run a single smoke test directly:

```bash
python3 smoke_tests/smoke_test_embedding_and_gen.py
```

Enable real integrations
------------------------
If you want the tests to call real cloud endpoints, set `UNMOCK=1` in the environment and
ensure the runtime is configured with appropriate credentials and endpoints.

Example (not recommended for CI):

```bash
UNMOCK=1 EMBEDDING_ENDPOINT=... GENERATIVE_ENDPOINT=... python3 smoke_tests/smoke_test_embedding_and_gen.py
```

Notes
- Tests are intentionally mocked to be deterministic and safe. They exercise error-handling
  paths (timeouts, retries) without requiring network access.
