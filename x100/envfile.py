"""Tiny KEY=VALUE loader shared by x100ctl and x100-bridge (no python-dotenv needed)."""

from __future__ import annotations

import os
from typing import List, Optional


def load_env_file(path: str) -> None:
    """Load KEY=VALUE lines into os.environ. Variables already set win."""
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def env_file_from_argv(argv: List[str], default: Optional[str] = "bridge.env") -> Optional[str]:
    """Find --env-file in argv (before argparse runs), else the default if it exists."""
    for i, arg in enumerate(argv):
        if arg == "--env-file" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--env-file="):
            return arg.split("=", 1)[1]
    return default if default and os.path.exists(default) else None
