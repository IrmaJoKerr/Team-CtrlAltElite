#!/usr/bin/env python3
"""
Import documents into Vertex AI RAG Corpus (Amcorpus).

Usage:
    # Import all PDFs from GCS bucket
    python3 scripts/import_to_rag_corpus.py --source gs://ambuckethack/

    # Import specific file
    python3 scripts/import_to_rag_corpus.py --source gs://ambuckethack/sop-example.pdf

    # Dry run (show what would be imported)
    python3 scripts/import_to_rag_corpus.py --source gs://ambuckethack/ --dry-run
"""
import os
import sys
import json
import argparse
import logging
import time
import requests
from google.auth import default
from google.auth.transport.requests import Request

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

# RAG Corpus configuration
PROJECT_ID = os.environ.get('PROJECT_ID', 'ctrlaltelite-484111')
REGION = 'us-west1'  # Your RAG corpus is in us-west1!
RAG_CORPUS_ID = '2305843009213693952'
RAG_CORPUS_NAME = f'projects/{PROJECT_ID}/locations/{REGION}/ragCorpora/{RAG_CORPUS_ID}'

# Chunk configuration for SOP documents
DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 100


def get_access_token():
    """Get OAuth2 access token using ADC."""
    creds, _ = default()
    creds.refresh(Request())
    return creds.token


def list_corpus_files():
    """List files already in the RAG corpus."""
    url = f"https://{REGION}-aiplatform.googleapis.com/v1beta1/{RAG_CORPUS_NAME}/ragFiles"
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    
    files = data.get('ragFiles', [])
    logging.info(f"Found {len(files)} files in corpus")
    return files


def import_from_gcs(gcs_uri: str, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP, dry_run: bool = False):
    """
    Import files from GCS into the RAG corpus.
    
    Args:
        gcs_uri: GCS URI (gs://bucket/path or gs://bucket/file.pdf)
        chunk_size: Characters per chunk
        chunk_overlap: Overlap between chunks
        dry_run: If True, only show what would be imported
    """
    url = f"https://{REGION}-aiplatform.googleapis.com/v1beta1/{RAG_CORPUS_NAME}/ragFiles:import"
    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json"
    }
    
    # Build import request
    payload = {
        "importRagFilesConfig": {
            "gcsSource": {
                "uris": [gcs_uri]
            },
            "ragFileChunkingConfig": {
                "chunkSize": chunk_size,
                "chunkOverlap": chunk_overlap
            }
        }
    }
    
    logging.info(f"Import config: chunk_size={chunk_size}, overlap={chunk_overlap}")
    logging.info(f"Source: {gcs_uri}")
    
    if dry_run:
        logging.info("[DRY RUN] Would send request:")
        logging.info(json.dumps(payload, indent=2))
        return None
    
    logging.info("Starting import operation...")
    resp = requests.post(url, headers=headers, json=payload)
    
    if resp.status_code != 200:
        logging.error(f"Import failed: {resp.status_code}")
        logging.error(resp.text)
        return None
    
    operation = resp.json()
    op_name = operation.get('name')
    logging.info(f"Import operation started: {op_name}")
    
    return operation


def check_operation_status(operation_name: str):
    """Check status of a long-running operation."""
    url = f"https://{REGION}-aiplatform.googleapis.com/v1beta1/{operation_name}"
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def wait_for_operation(operation_name: str, max_wait_seconds: int = 600):
    """Wait for operation to complete."""
    start = time.time()
    while time.time() - start < max_wait_seconds:
        status = check_operation_status(operation_name)
        
        if status.get('done'):
            if 'error' in status:
                logging.error(f"Operation failed: {status['error']}")
                return False
            logging.info("Operation completed successfully!")
            if 'response' in status:
                logging.info(f"Result: {json.dumps(status['response'], indent=2)}")
            return True
        
        logging.info("Operation in progress...")
        time.sleep(10)
    
    logging.error("Operation timed out")
    return False


def query_rag_corpus(query_text: str, top_k: int = 5, similarity_threshold: float = 0.5):
    """
    Query the RAG corpus directly using Vertex AI RAG API.
    
    This uses the native RAG retrieval - no need for pgvector.
    """
    url = f"https://{REGION}-aiplatform.googleapis.com/v1beta1/{RAG_CORPUS_NAME}:retrieveContexts"
    headers = {
        "Authorization": f"Bearer {get_access_token()}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "query": {
            "text": query_text,
            "ragRetrievalConfig": {
                "topK": top_k,
                "filter": {
                    "vectorSimilarityThreshold": similarity_threshold
                }
            }
        },
        "vertexRagStore": {
            "ragCorpora": [RAG_CORPUS_NAME]
        }
    }
    
    logging.info(f"Querying RAG corpus: '{query_text[:50]}...'")
    resp = requests.post(url, headers=headers, json=payload)
    
    if resp.status_code != 200:
        logging.error(f"Query failed: {resp.status_code}")
        logging.error(resp.text)
        return None
    
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description='Import documents to Vertex AI RAG Corpus')
    parser.add_argument('--source', type=str, help='GCS URI to import (gs://bucket/path)')
    parser.add_argument('--chunk-size', type=int, default=DEFAULT_CHUNK_SIZE, help='Chunk size in characters')
    parser.add_argument('--chunk-overlap', type=int, default=DEFAULT_CHUNK_OVERLAP, help='Chunk overlap in characters')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be imported without executing')
    parser.add_argument('--list', action='store_true', help='List files in corpus')
    parser.add_argument('--query', type=str, help='Test query against the corpus')
    parser.add_argument('--wait', action='store_true', help='Wait for import to complete')
    
    args = parser.parse_args()
    
    if args.list:
        files = list_corpus_files()
        for f in files:
            print(f"  - {f.get('displayName', f.get('name'))}")
        return
    
    if args.query:
        result = query_rag_corpus(args.query)
        if result:
            contexts = result.get('contexts', {}).get('contexts', [])
            print(f"\nFound {len(contexts)} relevant contexts:\n")
            for i, ctx in enumerate(contexts, 1):
                print(f"--- Result {i} ---")
                print(f"Source: {ctx.get('sourceUri', 'unknown')}")
                print(f"Score: {ctx.get('distance', 'N/A')}")
                print(f"Text: {ctx.get('text', '')[:300]}...")
                print()
        return
    
    if not args.source:
        parser.print_help()
        print("\nExamples:")
        print("  python3 scripts/import_to_rag_corpus.py --source gs://ambuckethack/")
        print("  python3 scripts/import_to_rag_corpus.py --list")
        print("  python3 scripts/import_to_rag_corpus.py --query 'customer onboarding procedure'")
        return
    
    operation = import_from_gcs(
        args.source,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        dry_run=args.dry_run
    )
    
    if operation and args.wait:
        op_name = operation.get('name')
        if op_name:
            wait_for_operation(op_name)


if __name__ == '__main__':
    main()
