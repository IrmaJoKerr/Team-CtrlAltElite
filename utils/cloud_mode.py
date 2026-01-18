import os
import sys
from typing import Iterable, Mapping, MutableMapping, Tuple, Any


def _arglist_to_str(argv: Iterable[str]) -> str:
    return " ".join(str(a).lower() for a in argv)


def _env_truthy(env: Mapping[str, str], key: str) -> bool:
    v = env.get(key)
    if v is None:
        return False
    return str(v).lower() in ("1", "true", "yes", "on")


def detect_cloud_mode(
    argv: Iterable[str] = None,
    env: Mapping[str, str] = None,
    globs: MutableMapping[str, Any] = None,
) -> Tuple[bool, str]:
    """Detect whether the process should run in cloud mode.

    Precedence (highest -> lowest):
      1. CLI args in ``argv`` (checks for flags like ``--cloud-mode``, ``cloudmode``, or ``--mode=cloud``)
      2. Environment variable ``CLOUD_MODE`` (truthy values: 1/true/yes/on)
      3. Module-global ``CLOUD_MODE`` variable provided via ``globs`` mapping
      4. Default: False

    Returns: (is_cloud_mode: bool, source: str) where source is one of
    'cli', 'env', 'global', or 'default'.
    """
    if argv is None:
        argv = sys.argv[1:]
    if env is None:
        env = os.environ
    if globs is None:
        globs = globals()

    # 1. CLI detection
    argstr = _arglist_to_str(argv)
    if (
        "--cloud-mode" in argstr
        or "cloudmode" in argstr
        or "--mode=cloud" in argstr
        or "cloud-mode" in argstr
    ):
        return True, "cli"

    # 2. Env var
    if _env_truthy(env, "CLOUD_MODE"):
        return True, "env"

    # 3. Global
    try:
        gval = globs.get("CLOUD_MODE")
        if gval:
            return True, "global"
    except Exception:
        pass

    return False, "default"
