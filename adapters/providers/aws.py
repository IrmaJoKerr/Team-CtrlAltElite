"""AWS secrets adapter (lightweight stub).

This stub does not call AWS SDKs. It provides a developer-friendly
fallback that reads environment variables. Replace with a real
`boto3`-backed implementation for production.
"""
from __future__ import annotations

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AWSSecretsAdapter:
    """Minimal AWS secrets adapter that reads env vars.

    Lookup order for secret name `X`:
      1. `X`
      2. `AWS_X`
      3. `AWS_SECRET_X`
    """

    def get(self, name: str) -> Optional[str]:
        if not name:
            return None
        val = os.environ.get(name)
        if val:
            return val
        for prefix in ('AWS_', 'AWS_SECRET_'):
            val = os.environ.get(f'{prefix}{name}')
            if val:
                return val
        logger.debug('AWSSecretsAdapter: secret %s not found in env', name)
        return None


def get_adapter():
    return AWSSecretsAdapter()
