import time
import logging
from typing import List, Dict, Any, Optional
import requests

LOG = logging.getLogger(__name__)


def upsert_points(
    points: List[Dict[str, Any]],
    collection: str,
    qdrant_url: str,
    dry_run: bool = False,
    retries: int = 3,
    backoff: float = 0.5,
    wait: bool = True,
) -> bool:
    """Upsert points into Qdrant collection with retries and optional dry-run.

    Points: list of {'id': ..., 'vector': [...], 'payload': {...}}
    Returns True on success, False on failure after retries.
    """
    if dry_run:
        LOG.info(
            "[DRY RUN] Would upsert %d points to Qdrant collection %s at %s",
            len(points),
            collection,
            qdrant_url,
        )
        for p in points[:5]:
            LOG.info(
                "  sample point id=%s meta=%s vector_len=%s",
                p.get("id"),
                {k: p.get("payload", {}).get(k) for k in ("title", "department")},
                len(p.get("vector") or []),
            )
        return True

    url = qdrant_url.rstrip("/") + f"/collections/{collection}/points"
    if wait:
        url += "?wait=true"

    payload = {"points": []}
    for p in points:
        payload["points"].append(
            {"id": p["id"], "vector": p["vector"], "payload": p.get("payload", {})}
        )

    attempt = 0
    cur_backoff = backoff
    while True:
        try:
            attempt += 1
            resp = requests.put(url, json=payload, timeout=30)
            if resp.status_code in (200, 201):
                return True
            LOG.error("Qdrant upsert failed: %s %s", resp.status_code, resp.text)
        except Exception as e:
            LOG.exception("Qdrant request failed (attempt %d): %s", attempt, e)

        if attempt >= retries:
            break
        time.sleep(cur_backoff)
        cur_backoff *= 2

    return False
