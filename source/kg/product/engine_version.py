from __future__ import annotations

import functools
import importlib.metadata
import subprocess
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).parents[3]


@functools.lru_cache(maxsize=1)
def engine_version() -> str:
    """Return a short version string identifying the running engine code.

    Resolution order:
      (a) git rev-parse --short HEAD from the repo root — identifies a checkout.
      (b) importlib.metadata package version prefixed with 'pkg:' — identifies an
          installed copy.  Distinguishes checkout-served from installed-served, which
          is the whole point of this stamp.
      (c) 'unknown' if both fail.

    Broad except is intentional here: this is the ONE place in the codebase where
    swallowing arbitrary errors is correct.  A version stamp must never crash the
    serving path; degrading to 'unknown' is always preferable to a 500.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(_repo_root()), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except Exception:  # noqa: BLE001 — intentional broad catch; see docstring
        pass
    try:
        return "pkg:" + importlib.metadata.version("supercontext")
    except Exception:  # noqa: BLE001 — intentional broad catch; see docstring
        pass
    return "unknown"
