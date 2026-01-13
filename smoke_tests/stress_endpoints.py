#!/usr/bin/env python3
"""
Stress test `rag-query` and `process-document` endpoints of the docintel-data-processor FastAPI app.
- Uses FastAPI `TestClient` to call endpoints without a network server.
- Monkeypatches storage client, DB connection, and auth session to avoid external deps.

Run from repository root:

python3 smoke_tests/stress_endpoints.py
"""
import os
import json
import base64
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi.testclient import TestClient


# Ensure TEST_MODE not required for rag-query path; set TEST_MODE before loading the module
# When UNMOCK=1, do not force TEST_MODE and skip monkeypatching so real services are used
if os.getenv('UNMOCK') != '1':
    os.environ['TEST_MODE'] = '1'

# Load the application module
import importlib.util
spec = importlib.util.spec_from_file_location('docintel_main', 'docintel-data-processor/main.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
app = mod.app
client = TestClient(app)

# Mock auth session similar to previous smoke test (applied only when not UNMOCK)
class DummyResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
    def json(self):
        return self._data
    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

class MockSession:
    def post(self, url, json=None, timeout=None):
        # If embedding endpoint call
        instances = (json or {}).get('instances') or []
        if instances and isinstance(instances, list):
            # Embedding: return small vector per instance
            preds = [[0.01, 0.02, 0.03] for _ in instances]
            return DummyResponse({'predictions': preds})
        # Generative: return simple text
        return DummyResponse({'predictions': [{'content': 'Mock generative answer.'}]})

# Mock storage: bucket().blob(name) -> Blob with needed methods
class MockBlob:
    def __init__(self, name, text='sample document content EMB_ text'):
        self.name = name
        self._text = text
    def download_as_text(self):
        return self._text
    def download_to_file(self, f):
        f.write(self._text.encode('utf-8'))
        return
    def exists(self):
        return True
    def delete(self):
        return

class MockBucket:
    def __init__(self, name):
        self.name = name
    def blob(self, name):
        return MockBlob(name)
    def list_blobs(self, prefix=None):
        return []
    def copy_blob(self, source_blob, destination_bucket, new_name):
        # Simulate copying by returning a new blob
        return MockBlob(new_name)

class MockStorageClient:
    def bucket(self, name):
        return MockBucket(name)

# Mock DB connection/cursor to avoid real DB
class MockCursor:
    def __init__(self):
        self._rows = []
        self.rowcount = 1
    def execute(self, *args, **kwargs):
        return
    def fetchone(self):
        return [1, 'orig.pdf', 'operations']
    def fetchall(self):
        return []
    def close(self):
        return

class MockConn:
    def cursor(self):
        return MockCursor()
    def commit(self):
        return
    def close(self):
        return
    def rollback(self):
        return

if os.getenv('UNMOCK') != '1':
    # Patch module objects for mocked run
    mod.storage_client = MockStorageClient()
    mod.get_authed_session = lambda: MockSession()
    mod.get_db_connection = lambda: MockConn()
    # Avoid psycopg2.extras.execute_values operations in mocks
    mod.execute_values = lambda *args, **kwargs: None
    # Ensure endpoints are set so code paths that check presence proceed
    mod.EMBEDDING_ENDPOINT = mod.EMBEDDING_ENDPOINT or 'projects/PROJECT/locations/REGION/endpoints/EMBEDDING_ID'
    mod.GENERATIVE_ENDPOINT = mod.GENERATIVE_ENDPOINT or 'projects/PROJECT/locations/REGION/endpoints/GEN_ID'
else:
    print('UNMOCK=1 set — running stress_endpoints without mocks (real GCS/DB/Vertex will be used)')

async def mock_async_post_with_retries(url, json=None, headers=None, timeout=None, retries=3, backoff_factor=1.0):
    instances = (json or {}).get('instances') or []
    if instances and isinstance(instances, list):
        # If first instance content contains 'EMB' -> embeddings
        if any('EMB' in (inst.get('content','') if isinstance(inst, dict) else str(inst)) for inst in instances):
            class R:
                def json(self):
                    return {'predictions': [[0.1,0.2,0.3] for _ in instances]}
            return R()
    # default generative response
    class R:
        def json(self):
            return {'predictions': [{'content': 'Mock generative answer.'}]}
    return R()

if os.getenv('UNMOCK') != '1':
    mod.async_post_with_retries = mock_async_post_with_retries

# Replace the generative helper with a deterministic coroutine to avoid parsing variability
async def mock_get_ai_metadata_suggestions(text):
    return {
        "title": {"suggested_value": "Mock Title", "justification": "test", "confidence_score": 0.9},
        "department": {"suggested_value": "Operations", "justification": "test", "confidence_score": 0.8},
        "process_type": {"suggested_value": "Incident Management", "justification": "test", "confidence_score": 0.7},
        "status": {"suggested_value": "Draft", "justification": "test", "confidence_score": 0.6}
    }

if os.getenv('UNMOCK') != '1':
    mod.get_ai_metadata_suggestions = mock_get_ai_metadata_suggestions

# Test payloads
AUTH_HEADER = {'Authorization': 'test-api-key'}

rag_payload = {'query': 'test query', 'top_k': 2}

# Build a fake Pub/Sub envelope for process-document
gcs_event = {'name': 'testfile.txt', 'bucket': 'mock-bucket'}
envelope = {'message': {'data': base64.b64encode(json.dumps(gcs_event).encode()).decode()}}

# Stress parameters
RAG_CONCURRENCY = 40
PROCESS_CONCURRENCY = 10

print('Starting stress test: rag-query', RAG_CONCURRENCY, 'concurrent requests')

def call_rag(i):
    try:
        r = client.post('/rag-query', json=rag_payload, headers=AUTH_HEADER)
        return (i, r.status_code, r.json())
    except Exception as e:
        return (i, 'exception', str(e))

print('Starting process-document stress test', PROCESS_CONCURRENCY, 'concurrent requests')

def call_process(i):
    try:
        r = client.post('/process-document', json=envelope)
        return (i, r.status_code, r.json())
    except Exception as e:
        return (i, 'exception', str(e))

# Run rag-query stress
start = time.time()
with ThreadPoolExecutor(max_workers=20) as ex:
    futures = [ex.submit(call_rag, i) for i in range(RAG_CONCURRENCY)]
    results = []
    for f in as_completed(futures):
        results.append(f.result())
end = time.time()
print('RAG results sample (first 10):')
for r in results[:10]:
    print(r)
print('RAG total', len(results), 'elapsed', end - start)

# Run process-document stress
start = time.time()
with ThreadPoolExecutor(max_workers=10) as ex:
    futures = [ex.submit(call_process, i) for i in range(PROCESS_CONCURRENCY)]
    results = []
    for f in as_completed(futures):
        results.append(f.result())
end = time.time()
print('PROCESS results:')
for r in results:
    print(r)
print('PROCESS total', len(results), 'elapsed', end - start)

print('Stress test complete')
