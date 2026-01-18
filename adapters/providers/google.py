"""Stub Google Cloud secrets adapter.

This is a placeholder implementation. Replace with a real adapter that
uses Google Secret Manager or ADC when implementing production support.
"""
from __future__ import annotations

from typing import Optional


class GoogleSecretsAdapter:
    def get(self, name: str) -> Optional[str]:
        raise NotImplementedError('Google Secrets Adapter not implemented. Implement adapters.providers.google.get_adapter to return a working adapter.')


def get_adapter():
    return GoogleSecretsAdapter()
