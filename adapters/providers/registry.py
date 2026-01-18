"""Registry and loader for provider-specific secrets adapter modules.

This module centralizes the list of supported providers and provides a
lazy loader to obtain a provider adapter instance.
"""
from __future__ import annotations

from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Keep this list in one place so CLI choices and loading stay in sync.
SUPPORTED_PROVIDERS = ['google', 'aws']


def get_adapter(provider: str):
    """Return an adapter instance for `provider` or None if not available.

    Provider modules should live under `adapters.providers.<provider>` and
    expose a `get_adapter()` function that returns an object with a `.get(name)` method.
    """
    if not provider:
        return None
    provider = provider.lower()
    if provider not in SUPPORTED_PROVIDERS:
        return None

    module_name = f'adapters.providers.{provider}'
    try:
        mod = __import__(module_name, fromlist=['*'])
        if hasattr(mod, 'get_adapter'):
            return mod.get_adapter()
    except Exception as e:
        logger.debug('Failed to import provider module %s: %s', module_name, e)
    return None
