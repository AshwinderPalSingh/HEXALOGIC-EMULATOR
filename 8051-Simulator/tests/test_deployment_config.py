from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUNICORN_CONFIG = ROOT / "gunicorn.conf.py"
RENDER_CONFIG = ROOT / "render.yaml"


def _load_gunicorn_config(env: dict[str, str]) -> dict[str, object]:
    source = GUNICORN_CONFIG.read_text()
    previous = os.environ.copy()
    os.environ.clear()
    os.environ.update(previous)
    os.environ.update(env)
    namespace: dict[str, object] = {}
    try:
        exec(compile(source, str(GUNICORN_CONFIG), "exec"), namespace)
    finally:
        os.environ.clear()
        os.environ.update(previous)
    return namespace


def test_memory_session_backend_forces_single_process_runtime():
    config = _load_gunicorn_config({
        "HEXLOGIC_SESSION_BACKEND": "memory",
        "GUNICORN_WORKERS": "8",
    })

    assert config["workers"] == 1
    assert config["worker_class"] == "gthread"
    assert config["threads"] == 4


def test_shared_redis_backend_allows_configured_worker_count():
    config = _load_gunicorn_config({
        "HEXLOGIC_SESSION_BACKEND": "redis",
        "REDIS_URL": "redis://example",
        "GUNICORN_WORKERS": "3",
    })

    assert config["workers"] == 3


def test_render_deploy_uses_shared_gunicorn_config():
    config = RENDER_CONFIG.read_text()

    assert "--config gunicorn.conf.py" in config
    assert "--workers 2" not in config
