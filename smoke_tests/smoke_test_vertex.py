#!/usr/bin/env python3
"""
Smoke test for Vertex integration (embeddings + generative) by mocking HTTP responses.
Run from repository root:

python3 smoke_tests/smoke_test_vertex.py

This script loads `docintel-data-processor/main.py` as a module, injects a `MockSession`
that simulates success, transient timeout, and permanent timeout behaviors, and
exercises `get_text_embeddings` and `get_ai_metadata_suggestions`.
"""
import asyncio
import os
import json
import importlib.util
import sys
import time
import requests
from types import SimpleNamespace

MODULE_PATH = 'docintel-data-processor/main.py'

# Load the module from file
spec = importlib.util.spec_from_file_location('docintel_main', MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class DummyResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.RequestException(f'Status {self.status_code}')

class MockSession:
    def __init__(self):
        # Track one-time timeouts
        self._counters = {}

    def post(self, url, json=None, timeout=None):
        # Inspect payload to decide behavior
        instances = json.get('instances', []) if isinstance(json, dict) else []
        content = ''
        for inst in instances:
            if isinstance(inst, dict):
                content += (inst.get('content') or '')
            else:
                content += str(inst)

        # Decide whether this is embedding or generative based on content markers
        is_embed = 'EMB_' in content
        is_gen = 'GEN_' in content

        # Embedding behaviors
        if is_embed:
            if 'TIMEOUT_ALWAYS' in content:
                raise requests.exceptions.Timeout('simulated permanent timeout')
            if 'TIMEOUT_ONCE' in content:
                k = 'emb_once'
                self._counters.setdefault(k, 0)
                self._counters[k] += 1
                if self._counters[k] == 1:
                    raise requests.exceptions.Timeout('simulated transient timeout')
            # Success: return a vector per instance (length=3)
            preds = [[0.1, 0.2, 0.3] for _ in instances]
            return DummyResponse({'predictions': preds})

        # Generative behaviors
        if is_gen:
            if 'TIMEOUT_ALWAYS' in content:
                raise requests.exceptions.Timeout('simulated permanent timeout')
            if 'TIMEOUT_ONCE' in content:
                k = 'gen_once'
                self._counters.setdefault(k, 0)
                self._counters[k] += 1
                if self._counters[k] == 1:
                    raise requests.exceptions.Timeout('simulated transient timeout')

            # If caller asks for JSON return, return a JSON string payload that the code will parse
            if 'RETURN_JSON' in content:
                # Provide suggested fields expected by get_ai_metadata_suggestions
                payload = {
                    'title': {'suggested_value': 'Mock Title', 'justification': 'Because smoke test', 'confidence_score': 0.9},
                    'department': {'suggested_value': 'Operations', 'justification': 'Detected operations content', 'confidence_score': 0.8},
                    'process_type': {'suggested_value': 'Incident Management', 'justification': 'Keywords present', 'confidence_score': 0.7},
                    'status': {'suggested_value': 'Draft', 'justification': 'Default', 'confidence_score': 0.6}
                }
                # The generative endpoint often returns text content; we return a JSON string
                return DummyResponse({'predictions': [ {'content': json.dumps(payload)} ]})

            # Default generative response - plain text answer
            return DummyResponse({'predictions': [ {'content': 'Mock Answer: processed snippets.'} ]})

        # Fallback
        return DummyResponse({'predictions': []})


async def run_embedding_test(session, texts, expect_timeout=False):
    # Inject mock session into module via get_authed_session monkeypatch
    orig_get = mod.get_authed_session
    if os.getenv('UNMOCK') != '1':
        mod.get_authed_session = lambda: session
    try:
        try:
            vecs = await mod.get_text_embeddings(texts)
            print(f"Embedding success for texts={texts}: vectors_count={len(vecs)}")
        except Exception as e:
            print(f"Embedding call for texts={texts} raised: {type(e).__name__}: {e}")
    finally:
        if os.getenv('UNMOCK') != '1':
            mod.get_authed_session = orig_get


async def run_gen_test(session, text, expect_timeout=False):
    orig_get = mod.get_authed_session
    if os.getenv('UNMOCK') != '1':
        mod.get_authed_session = lambda: session
    try:
        try:
            parsed = await mod.get_ai_metadata_suggestions(text)
            print(f"Generative success for text='{text[:40]}': {parsed}")
        except Exception as e:
            print(f"Generative call for text='{text[:40]}' raised: {type(e).__name__}: {e}")
    finally:
        if os.getenv('UNMOCK') != '1':
            mod.get_authed_session = orig_get


async def main():
    session = MockSession()

    # Monkeypatch the module's async POST helper so httpx calls are mocked
    import httpx
    if os.getenv('UNMOCK') != '1':
        async def _mock_async_post_with_retries(url, json=None, headers=None, timeout=None, retries=3, backoff_factor=1.0):
            # Run the synchronous MockSession.post in a thread to simulate network behavior
            def call():
                return session.post(url, json=json, timeout=timeout)
            try:
                resp = await asyncio.to_thread(call)
            except requests.exceptions.Timeout as e:
                raise httpx.ReadTimeout(str(e))
            except requests.exceptions.RequestException as e:
                raise httpx.RequestError(str(e))

            # Return an object with .json() similar to httpx response
            return SimpleNamespace(json=lambda: resp.json())

        mod.async_post_with_retries = _mock_async_post_with_retries
    else:
        print('UNMOCK=1 set — using real HTTP helpers and auth from module')

    tests = [
        run_embedding_test(session, ["EMB_NORMAL: Hello world"]),
        run_embedding_test(session, ["EMB_TIMEOUT_ONCE: TIMEOUT_ONCE_EMB"]),
        run_embedding_test(session, ["EMB_TIMEOUT_ALWAYS: TIMEOUT_ALWAYS_EMB"]),
        run_gen_test(session, "GEN_NORMAL: simple document text"),
        run_gen_test(session, "GEN_TIMEOUT_ONCE: TIMEOUT_ONCE_GEN RETURN_JSON"),
        run_gen_test(session, "GEN_TIMEOUT_ALWAYS: TIMEOUT_ALWAYS_GEN RETURN_JSON"),
    ]

    await asyncio.gather(*tests)


if __name__ == '__main__':
    asyncio.run(main())
