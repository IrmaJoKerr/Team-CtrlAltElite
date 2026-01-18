"""Google Cloud secrets adapter (lightweight stub).

This stub attempts a local-friendly lookup for secrets when running in
cloud mode during development. It does NOT call GCP APIs. Replace with
an implementation that uses `google-cloud-secret-manager` for real use.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GoogleSecretsAdapter:
    """A minimal adapter that reads environment variables as a fallback.

    Lookup order for secret name `X`:
      1. `X` (literal env var)
      2. `GCP_X`
      3. `GOOGLE_X`
    """

    def get(self, name: str) -> Optional[str]:
        if not name:
            return None
        # direct match
        val = os.environ.get(name)
        if val:
            return val
        # provider-prefixed fallbacks
        for prefix in ('GCP_', 'GOOGLE_'):
            val = os.environ.get(f'{prefix}{name}')
            if val:
                return val
        logger.debug('GoogleSecretsAdapter: secret %s not found in env', name)
        return None


def get_adapter():
    return GoogleSecretsAdapter()
