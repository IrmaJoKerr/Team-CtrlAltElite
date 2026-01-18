import os
import json
import logging
import asyncio
from typing import List, Dict, Any, Optional

import httpx

LOGGER = logging.getLogger(__name__)


async def _post_with_retries(url: str, json_payload: dict, headers: Optional[dict] = None, timeout: Optional[tuple] = None, retries: int = 3, backoff_factor: float = 1.0):
    attempt = 0
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=json_payload, headers=headers or {}, timeout=timeout)
                resp.raise_for_status()
                return resp
        except httpx.ReadTimeout:
            attempt += 1
            if attempt > retries:
                raise
            await asyncio.sleep(backoff_factor * (2 ** (attempt - 1)))
        except httpx.RequestError:
            attempt += 1
            if attempt > retries:
                raise
            await asyncio.sleep(backoff_factor * (2 ** (attempt - 1)))


async def generate_answer(prompt: str, snippets: List[Dict[str, Any]]) -> str:
    """Generate an answer for a prompt using configured generative endpoint.

    Local-first: if no `GENERATIVE_ENDPOINT` is configured in env, return
    the concatenated snippets as a safe default.
    """
    GENERATIVE_ENDPOINT = os.environ.get('GENERATIVE_ENDPOINT')
    REGION = os.environ.get('REGION', 'us-central1')

    context_text = '\n\n'.join([s.get('snippet', '') for s in snippets])[:4000]
    prompt_payload = prompt

    if not GENERATIVE_ENDPOINT:
        LOGGER.warning('GENERATIVE_ENDPOINT not configured; using snippets as answer')
        return context_text or ''

    # Build URL similar to previous code expectations
    try:
        if GENERATIVE_ENDPOINT.startswith('projects/'):
            url = f"https://{REGION}-aiplatform.googleapis.com/v1/{GENERATIVE_ENDPOINT}:predict"
        else:
            url = f"https://{REGION}-aiplatform.googleapis.com/v1/{GENERATIVE_ENDPOINT}:predict"

        payload = {"instances": [{"content": prompt_payload}]}
        resp = await _post_with_retries(url, json_payload=payload, headers={}, timeout=(5.0, 120.0), retries=3, backoff_factor=1.0)
        data = resp.json()
        preds = data.get('predictions') or data.get('outputs') or []
        text_response = None
        if isinstance(preds, list) and len(preds) > 0:
            first = preds[0]
            if isinstance(first, dict):
                for k in ('content','text','output','generated_text','candidates'):
                    if k in first:
                        if k == 'candidates' and isinstance(first[k], list) and len(first[k])>0:
                            cand = first[k][0]
                            if isinstance(cand, dict):
                                text_response = cand.get('content') or cand.get('text')
                            else:
                                text_response = str(cand)
                            break
                        else:
                            val = first[k]
                            if isinstance(val, str):
                                text_response = val
                                break
                            elif isinstance(val, dict) and 'text' in val:
                                text_response = val['text']
                                break
            elif isinstance(first, str):
                text_response = first

        if not text_response and isinstance(preds, list) and len(preds) > 0:
            text_response = json.dumps(preds[0])

        return text_response or context_text or ''
    except Exception:
        LOGGER.exception('Generative model failed; returning snippets as answer')
        return context_text or ''
import os
import json
import logging
import httpx

REGION = os.environ.get('REGION', 'us-central1')
GENERATIVE_ENDPOINT = os.environ.get('GENERATIVE_ENDPOINT')


async def generate_answer(prompt: str, snippets: list, headers: dict | None = None, timeout=(5.0, 120.0)) -> str:
    """Adapter for generative model calls.

    Local-first: if `GENERATIVE_ENDPOINT` is not configured, return the
    concatenated snippets as the answer (same behavior as previous code).
    If configured, make a simple async POST and try to extract a textual
    response from common response shapes.
    """
    context_text = '\n\n'.join([s.get('snippet', '') for s in snippets])[:4000]
    if not GENERATIVE_ENDPOINT:
        logging.warning('GENERATIVE_ENDPOINT not configured; using snippets as answer')
        return context_text or ""

    url = f"https://{REGION}-aiplatform.googleapis.com/v1/{GENERATIVE_ENDPOINT}:predict"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={"instances": [{"content": prompt}]}, headers=headers or {}, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

    preds = data.get('predictions') or data.get('outputs') or []
    text_response = None
    if isinstance(preds, list) and len(preds) > 0:
        first = preds[0]
        if isinstance(first, dict):
            for k in ('content', 'text', 'output', 'generated_text', 'candidates'):
                if k in first:
                    if k == 'candidates' and isinstance(first[k], list) and len(first[k]) > 0:
                        cand = first[k][0]
                        if isinstance(cand, dict):
                            text_response = cand.get('content') or cand.get('text')
                        else:
                            text_response = str(cand)
                        break
                    else:
                        val = first[k]
                        if isinstance(val, str):
                            text_response = val
                            break
                        elif isinstance(val, dict) and 'text' in val:
                            text_response = val['text']
                            break
        elif isinstance(first, str):
            text_response = first

    if not text_response and isinstance(preds, list) and len(preds) > 0:
        try:
            text_response = json.dumps(preds[0])
        except Exception:
            text_response = None

    return text_response or (context_text or "")
