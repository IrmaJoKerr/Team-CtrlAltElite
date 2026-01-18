"""CLI and env parsing helpers for sync scripts.

Provides a standardized parser and `get_effective_config` that scripts
can call to determine runtime mode (cloud vs local), whether to actually
apply changes (confirm vs simulate), and common knobs like batch size
and Qdrant connection info.

Precedence for cloud mode: CLI flag `--cloud-mode` > env `CLOUD_MODE` > default False.
Default behavior: simulate (no writes) unless `--confirm` is passed.
"""

from __future__ import annotations

import argparse
import os
from typing import Dict, Any, Iterable, Optional

from utils.cloud_mode import detect_cloud_mode
from utils.config import Config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=True)
    # runtime mode: local or cloud (required)
    p.add_argument(
        "--mode",
        "-m",
        choices=["local", "cloud"],
        required=True,
        help='Runtime mode: "local" (env-based) or "cloud" (use cloud secret providers)',
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm and apply changes (default is simulate)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=int(os.environ.get("BATCH_SIZE", "100")),
        help="Number of items to process per batch",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=int(os.environ.get("MAX_RETRIES", "3")),
        help="Max retries for transient failures",
    )
    p.add_argument(
        "--qdrant-url",
        type=str,
        default=os.environ.get("QDRANT_URL"),
        help="Qdrant URL (env QDRANT_URL)",
    )
    p.add_argument(
        "--qdrant-api-key",
        type=str,
        default=os.environ.get("QDRANT_API_KEY"),
        help="Qdrant API key (env QDRANT_API_KEY)",
    )
    p.add_argument(
        "--log-file",
        type=str,
        default=os.environ.get("LOG_FILE"),
        help="Optional log file path",
    )
    p.add_argument(
        "--simulate", action="store_true", help="Explicitly simulate (no writes)"
    )
    # secret provider override: choices are loaded from adapter registry
    try:
        from adapters.providers.registry import SUPPORTED_PROVIDERS
    except Exception:
        SUPPORTED_PROVIDERS = None
    p.add_argument(
        "--secret-provider",
        choices=SUPPORTED_PROVIDERS,
        help="Explicit cloud secret provider (overrides SECRET_PROVIDER env)",
    )
    return p


def get_effective_config(
    argv: Optional[Iterable[str]] = None, env: Optional[Dict[str, str]] = None
) -> Config:
    """Parse CLI args and environment to produce an effective configuration dict.

    Args:
        argv: optional list of CLI args (defaults to sys.argv)
        env: optional env mapping (defaults to os.environ)

    Returns: dict with keys: cloud_mode (bool), simulate (bool), confirm (bool), batch_size, max_retries,
             qdrant_url, qdrant_api_key, log_file, args (raw namespace)
    """
    if env is None:
        env = os.environ

    parser = build_parser()
    ns = parser.parse_args(list(argv) if argv is not None else None)

    # Cloud mode enforced via required --mode flag (local or cloud)
    cloud_mode = True if getattr(ns, "mode", "local") == "cloud" else False
    cloud_source = "cli"

    # simulate default: True unless --confirm passed; explicit --simulate overrides
    if ns.confirm:
        simulate = False
    elif ns.simulate:
        simulate = True
    else:
        simulate = True

    cfg = Config.from_namespace(ns)
    return cfg
