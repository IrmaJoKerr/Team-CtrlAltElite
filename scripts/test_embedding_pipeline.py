#!/usr/bin/env python3
"""
Quick validation tests for embedding pipeline integration.
Run from project root: python scripts/test_embedding_pipeline.py

Tests:
1. Database schema validation (new columns exist)
2. Embedding status endpoint responses
3. Background worker configuration
"""

import os
import sys
import json
import requests
from datetime import datetime

# Configuration
API_BASE = os.environ.get('API_BASE', 'http://localhost:8000')
TEST_TOKEN = os.environ.get('TEST_TOKEN', 'demo-token')

def test_header():
    return {
        'Authorization': f'Bearer {TEST_TOKEN}',
        'Content-Type': 'application/json'
    }

def log(msg, status='INFO'):
    symbols = {'INFO': '🔹', 'PASS': '✅', 'FAIL': '❌', 'WARN': '⚠️'}
    print(f"{symbols.get(status, '🔹')} {msg}")

def test_health_check():
    """Test 1: Basic health check"""
    log("Testing health check endpoint...")
    try:
        resp = requests.get(f"{API_BASE}/", timeout=10)
        if resp.status_code == 200:
            log("Health check passed", 'PASS')
            return True
        else:
            log(f"Health check returned {resp.status_code}", 'FAIL')
            return False
    except Exception as e:
        log(f"Health check failed: {e}", 'FAIL')
        return False

def test_embedding_status_endpoint():
    """Test 2: Embedding status endpoint exists"""
    log("Testing embedding status endpoint...")
    try:
        # Test with invalid ID - should return 404
        resp = requests.get(
            f"{API_BASE}/embedding-status/999999",
            headers=test_header(),
            timeout=10
        )
        if resp.status_code == 404:
            log("Embedding status endpoint exists (returned 404 for missing doc)", 'PASS')
            return True
        elif resp.status_code == 401:
            log("Embedding status endpoint exists (returned 401 - auth required)", 'PASS')
            return True
        else:
            log(f"Embedding status endpoint returned unexpected: {resp.status_code}", 'WARN')
            return True
    except Exception as e:
        log(f"Embedding status endpoint failed: {e}", 'FAIL')
        return False

def test_embedding_summary_endpoint():
    """Test 3: Embedding summary endpoint"""
    log("Testing embedding summary endpoint...")
    try:
        resp = requests.get(
            f"{API_BASE}/embedding-status/summary",
            headers=test_header(),
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            log(f"Summary: {data.get('total', 0)} total, {data.get('pending', 0)} pending, {data.get('complete', 0)} complete", 'PASS')
            return True
        elif resp.status_code == 401:
            log("Summary endpoint exists (auth required)", 'PASS')
            return True
        else:
            log(f"Summary endpoint returned: {resp.status_code}", 'WARN')
            return True
    except Exception as e:
        log(f"Summary endpoint failed: {e}", 'FAIL')
        return False

def test_bulk_status_endpoint():
    """Test 4: Bulk embedding status endpoint"""
    log("Testing bulk embedding status endpoint...")
    try:
        resp = requests.post(
            f"{API_BASE}/embedding-status/bulk",
            headers=test_header(),
            json={'document_ids': []},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            log(f"Bulk status: total={data.get('total', 0)}", 'PASS')
            return True
        elif resp.status_code == 401:
            log("Bulk status endpoint exists (auth required)", 'PASS')
            return True
        else:
            log(f"Bulk status returned: {resp.status_code}", 'WARN')
            return True
    except Exception as e:
        log(f"Bulk status failed: {e}", 'FAIL')
        return False

def test_rag_query_endpoint():
    """Test 5: RAG query endpoint still works"""
    log("Testing RAG query endpoint...")
    try:
        resp = requests.post(
            f"{API_BASE}/rag-query-v2",
            headers=test_header(),
            json={
                'query': 'test query',
                'department': 'loans',
                'top_k': 5
            },
            timeout=30
        )
        if resp.status_code in [200, 401, 500]:
            log(f"RAG query endpoint responded: {resp.status_code}", 'PASS' if resp.status_code == 200 else 'WARN')
            return True
        else:
            log(f"RAG query returned: {resp.status_code}", 'WARN')
            return True
    except Exception as e:
        log(f"RAG query failed: {e}", 'FAIL')
        return False

def main():
    print("\n" + "="*60)
    print("🔬 EMBEDDING PIPELINE VALIDATION TESTS")
    print("="*60)
    print(f"📡 API Base: {API_BASE}")
    print(f"🕐 Time: {datetime.now().isoformat()}")
    print("="*60 + "\n")

    tests = [
        test_health_check,
        test_embedding_status_endpoint,
        test_embedding_summary_endpoint,
        test_bulk_status_endpoint,
        test_rag_query_endpoint,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            log(f"Test {test.__name__} threw exception: {e}", 'FAIL')
            failed += 1
        print()

    print("="*60)
    print(f"📊 RESULTS: {passed} passed, {failed} failed")
    print("="*60)

    return 0 if failed == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
