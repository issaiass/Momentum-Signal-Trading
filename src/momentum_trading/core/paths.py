"""
core/paths.py

Single source of truth for resolving where config.yaml,
data/, and logs/ actually live. Before this module existed, every path in
the codebase ("data/...", "config.yaml", "trades_log.txt") was a bare
relative string, which only worked if the process happened to be launched
with its CWD set to the project root — fragile, and a real risk once the
package can be installed/imported from anywhere.

Resolution order:
  1. MOMENTUM_TRADING_ROOT env var, if set (explicit override — useful for
     Docker/deployment where you want an unambiguous, non-guessed root)
  2. Walk up from this file's location looking for pyproject.toml (works for
     an editable install / running from a source checkout)
  3. Fall back to the current working directory (preserves old behavior for
     anyone still invoking scripts the original way)
"""

from __future__ import annotations

import os
from pathlib import Path


def _find_project_root() -> Path:
    env_override = os.environ.get("MOMENTUM_TRADING_ROOT")
    if env_override:
        return Path(env_override).resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent

    return Path.cwd()


PROJECT_ROOT: Path = _find_project_root()


def data_dir() -> Path:
    """Where trade logs, snapshots, and lock/flag files live."""
    d = PROJECT_ROOT / "data"
    d.mkdir(exist_ok=True)
    return d


def config_path(filename: str = "config.yaml") -> Path:
    return PROJECT_ROOT / filename


def logs_dir() -> Path:
    d = PROJECT_ROOT / "logs"
    d.mkdir(exist_ok=True)
    return d
