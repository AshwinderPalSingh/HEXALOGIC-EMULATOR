#!/usr/bin/env python3
"""Run HexaLogic locally through Flask.

This is the Option B path for users who want the same backend-driven app shell
without waiting for the deployed website. It intentionally uses Flask's local
development server; production deployments should keep using Gunicorn/Render.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.index import app


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = _env_int("PORT", 5001)
    debug = os.environ.get("FLASK_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
    app.run(host=host, port=port, debug=debug)
