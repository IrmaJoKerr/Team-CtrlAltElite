#!/usr/bin/env python3
"""Check Vertex endpoint access using a service account JSON.

Writes HTTP status and response body to stdout for diagnosis.
"""
import os
import sys
import json
import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request

KEY = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
EMB = os.environ.get('EMBEDDING_ENDPOINT')
REGION = os.environ.get('REGION', 'us-central1')

if not KEY or not EMB:
    print('Missing GOOGLE_APPLICATION_CREDENTIALS or EMBEDDING_ENDPOINT env vars', file=sys.stderr)
    sys.exit(2)

scopes = ['https://www.googleapis.com/auth/cloud-platform']
try:
    creds = service_account.Credentials.from_service_account_file(KEY, scopes=scopes)
    req = Request()
    creds.refresh(req)
    token = creds.token
    print('Obtained access token (truncated):', token[:40] + '...')
except Exception as e:
    print('Failed to obtain access token:', e, file=sys.stderr)
    raise

url = f'https://{REGION}-aiplatform.googleapis.com/v1/{EMB}:predict'
payload = {"instances": [{"content": "test"}]}
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

try:
    with httpx.Client(timeout=30.0) as client:
        r = client.post(url, json=payload, headers=headers)
        print('HTTP', r.status_code)
        try:
            print('BODY:', json.dumps(r.json(), indent=2))
        except Exception:
            print('RAW BODY:', r.text)
        r.raise_for_status()
except Exception as e:
    print('Request error:', repr(e), file=sys.stderr)
    sys.exit(1)

print('Request succeeded')
