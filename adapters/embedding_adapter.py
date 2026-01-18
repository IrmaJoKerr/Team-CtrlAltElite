"""Pluggable embedding adapter with three modes: stub, local, hosted.

Usage:
- Set environment variable `EMBEDDING_PROVIDER` to one of: `stub`, `local`, `hosted`, or `auto`.
  - `stub`: deterministic hash-based vectors for tests/CI.
  - `local`: uses `sentence-transformers` (all-MiniLM-L6-v2 by default).
  - `hosted`: scaffold for calling a hosted embedding API (implement provider call and set creds).
+  - `auto` (default): prefer `local` if sentence-transformers available, else fall back to `stub`.

This file keeps a small, well-documented surface so switching providers is a config change.
"""

from typing import List
import os
import hashlib
import time
import math

EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "auto").lower()
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))


def _hash_vector(text: str, dim: int = EMBEDDING_DIM) -> List[float]:
    # deterministic pseudo-vector for tests; not semantically meaningful
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vals = []
    for i in range(dim):
        b = h[i % len(h)]
        vals.append((b / 255.0) * 2.0 - 1.0)
    return vals


class StubEmbedder:
    def __init__(self, dim: int = EMBEDDING_DIM):
        self.dim = dim

    def encode(self, texts: List[str]) -> List[List[float]]:
        return [_hash_vector(t, dim=self.dim) for t in texts]


class LocalEmbedder:
    def __init__(self, model_name: str = EMBEDDING_MODEL):
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as e:
            raise RuntimeError("sentence-transformers not available") from e
        self.model = SentenceTransformer(model_name)

    def encode(self, texts: List[str]) -> List[List[float]]:
        out = self.model.encode(texts, show_progress_bar=False)
        # ensure list[float]
        return [list(map(float, v)) for v in out]


class HostedEmbedder:
    def __init__(self):
        # Scaffold: implement provider-specific auth and calls here.
        # Example env vars to set when using a hosted provider:
        # - HOSTED_EMBEDDING_API_KEY
        # - HOSTED_EMBEDDING_ENDPOINT
        self.endpoint = os.environ.get("HOSTED_EMBEDDING_ENDPOINT")
        self.api_key = os.environ.get("HOSTED_EMBEDDING_API_KEY")
        if not self.endpoint or not self.api_key:
            raise RuntimeError(
                "Hosted embedding provider not configured. Set HOSTED_EMBEDDING_ENDPOINT and HOSTED_EMBEDDING_API_KEY"
            )

    def encode(self, texts: List[str]) -> List[List[float]]:
        # Minimal scaffold: implement actual HTTP calls with batching and retries.
        raise NotImplementedError(
            "Hosted embedder not implemented; implement provider call here"
        )


def _select_embedder():
    provider = EMBEDDING_PROVIDER
    if provider == "stub":
        return StubEmbedder()
    if provider == "local":
        try:
            return LocalEmbedder()
        except Exception:
            # fall back to stub if local model cannot be loaded
            return StubEmbedder()
    if provider == "hosted":
        return HostedEmbedder()
    # auto: prefer local when available
    try:
        return LocalEmbedder()
    except Exception:
        return StubEmbedder()


_EMBEDDER = _select_embedder()


def get_embeddings(texts: List[str]) -> List[List[float]]:
    """Synchronous API to generate embeddings for a list of texts.

    For heavy workloads, call from a thread via `asyncio.to_thread(get_embeddings, texts)`.
    """
    # Naive retry around embedder.encode for transient errors (exponential backoff)
    max_attempts = 3
    backoff = 0.5
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return _EMBEDDER.encode(texts)
        except Exception as e:
            last_exc = e
            if attempt == max_attempts:
                raise
            time.sleep(backoff)
            backoff *= 2
    raise last_exc
