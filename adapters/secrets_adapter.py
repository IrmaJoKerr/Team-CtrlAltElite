"""Platform-agnostic secrets adapter scaffold.

This module provides a minimal adapter interface and a safe default
implementation that reads secrets from the environment. A cloud-backed
adapter can be implemented later and selected at runtime when
`cloud_mode=True`.

Usage:
    from adapters.secrets_adapter import get_db_password
    pw = get_db_password(cloud_mode=True, secret_name='projects/.../secrets/DB_PASSWORD')
"""
from __future__ import annotations

import os
import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)

# Registry of providers is maintained in adapters.providers.registry
try:
    from adapters.providers.registry import SUPPORTED_PROVIDERS, get_adapter as registry_get_adapter
except Exception:
    SUPPORTED_PROVIDERS = ['google', 'aws']
    registry_get_adapter = None


class SecretsAdapter(ABC):
    @abstractmethod
    def get(self, name: str) -> Optional[str]:
        """Return the secret value for `name` or None if not found."""


class EnvSecretsAdapter(SecretsAdapter):
    def get(self, name: str) -> Optional[str]:
        # Primary local-first behaviour: read direct env var
        return os.environ.get(name)


class StubCloudSecretsAdapter(SecretsAdapter):
    def __init__(self, provider: Optional[str] = None):
        self.provider = provider

    def get(self, name: str) -> Optional[str]:
        # Placeholder: do not import cloud SDKs here to keep this package
        # platform-agnostic. Implement provider-specific logic in a
        # separate module (e.g. adapters/secrets_adapter_gcp.py) and return
        # an adapter instance via `get_secrets_adapter` below.
        raise NotImplementedError(
            f"Cloud secrets fetcher for provider={self.provider} is not implemented."
        )


def get_secrets_adapter(cloud_mode: bool = False, config: Optional[object] = None) -> SecretsAdapter:
    """Return an appropriate `SecretsAdapter`.

    - If `cloud_mode` is False: return `EnvSecretsAdapter` (local-first).
    - If `cloud_mode` is True: check `SECRET_PROVIDER` env var and attempt
      to create a cloud adapter. Currently returns a `StubCloudSecretsAdapter`.
    """
    if not cloud_mode:
        return EnvSecretsAdapter()
    # Precedence: explicit config.secret_provider -> env SECRET_PROVIDER -> error
    provider = None
    source = None
    if config is not None and getattr(config, 'secret_provider', None):
        provider = getattr(config, 'secret_provider')
        source = 'config'
    elif os.environ.get('SECRET_PROVIDER'):
        provider = os.environ.get('SECRET_PROVIDER')
        source = 'env'

    if not provider:
        raise RuntimeError('Cloud mode selected but no secret provider configured. Set --secret-provider or SECRET_PROVIDER env var.')

    provider = provider.lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise RuntimeError(f'Unsupported secret provider "{provider}". Supported: {SUPPORTED_PROVIDERS}')

    logger.info('Using secret provider %s (source=%s)', provider, source)

    # Try to obtain a provider-specific adapter via the registry if present
    if registry_get_adapter is not None:
        adapter = registry_get_adapter(provider)
        if adapter is not None:
            return adapter

    return StubCloudSecretsAdapter(provider=provider)


def get_db_password(cloud_mode: bool = False, secret_name: Optional[str] = None, config: Optional[object] = None) -> str:
    """Return DB password according to local-first rules.

    Precedence:
    1. `DB_PASSWORD` env var (always preferred)
    2. If `cloud_mode` is True and a provider adapter is configured,
       attempt to fetch `secret_name` (or the literal 'DB_PASSWORD') via
       that adapter.
    3. Raise RuntimeError if not available.
    """
    env_pw = os.environ.get('DB_PASSWORD')
    if env_pw:
        return env_pw

    adapter = get_secrets_adapter(cloud_mode=cloud_mode, config=config)
    lookup_name = secret_name or 'DB_PASSWORD'
    try:
        val = adapter.get(lookup_name)
    except NotImplementedError:
        val = None

    if val:
        return val

    raise RuntimeError(
        'DB password not found. Set DB_PASSWORD env var or implement a cloud secrets adapter.'
    )
