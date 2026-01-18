"""Runtime configuration object shared by orchestrator and modules.

Provide an immutable `Config` dataclass to pass runtime mode and common
settings to modules. This avoids globals and makes unit testing easier.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Any, Iterable


@dataclass(frozen=True)
class Config:
    mode: str  # 'local' or 'cloud'
    cloud_mode: bool
    simulate: bool
    confirm: bool
    batch_size: int
    max_retries: int
    qdrant_url: Optional[str]
    qdrant_api_key: Optional[str]
    log_file: Optional[str]
    secret_provider: Optional[str] = None
    args: Optional[Any] = None

    @classmethod
    def from_namespace(cls, ns: Any, simulate_default: bool = True) -> 'Config':
        mode = getattr(ns, 'mode', 'local')
        cloud_mode = True if mode == 'cloud' else False
        # explicit simulate flag or default behavior
        if getattr(ns, 'confirm', False):
            simulate = False
        elif getattr(ns, 'simulate', None) is not None:
            simulate = getattr(ns, 'simulate')
        else:
            simulate = simulate_default

        return cls(
            mode=mode,
            cloud_mode=cloud_mode,
            simulate=simulate,
            confirm=bool(getattr(ns, 'confirm', False)),
            batch_size=int(getattr(ns, 'batch_size', 100)),
            max_retries=int(getattr(ns, 'max_retries', 3)),
            qdrant_url=getattr(ns, 'qdrant_url', None),
            qdrant_api_key=getattr(ns, 'qdrant_api_key', None),
            log_file=getattr(ns, 'log_file', None),
            secret_provider=getattr(ns, 'secret_provider', None),
            args=ns,
        )
