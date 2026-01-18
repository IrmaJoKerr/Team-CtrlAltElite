"""Stub AWS secrets adapter.

This is a placeholder implementation. Replace with a real adapter that
uses AWS Secrets Manager when implementing production support.
"""
from __future__ import annotations

from typing import Optional


class AWSSecretsAdapter:
    def get(self, name: str) -> Optional[str]:
        raise NotImplementedError('AWS Secrets Adapter not implemented. Implement adapters.providers.aws.get_adapter to return a working adapter.')


def get_adapter():
    return AWSSecretsAdapter()
