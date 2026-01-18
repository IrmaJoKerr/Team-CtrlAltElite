import asyncio
from typing import List

async def get_text_embeddings(texts: List[str]) -> List[List[float]]:
    """Async wrapper around the embedding adapter's sync API.

    Runs the synchronous `adapters.embedding_adapter.get_embeddings` in a thread
    so callers can `await` it from async handlers.
    """
    try:
        from adapters.embedding_adapter import get_embeddings as _adapter_get_embeddings
    except Exception as e:
        raise RuntimeError('No embedding adapter available. Implement adapters.embedding_adapter.get_embeddings') from e

    # Run adapter in a thread to avoid blocking the event loop if adapter is sync
    try:
        vecs = await asyncio.to_thread(_adapter_get_embeddings, texts)
        return vecs
    except Exception as e:
        raise
