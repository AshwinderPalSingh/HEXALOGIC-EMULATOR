import os


session_backend = os.getenv("HEXLOGIC_SESSION_BACKEND", "memory").strip().lower()
redis_url = os.getenv("REDIS_URL", "").strip()
shared_session_backend = session_backend == "redis" and bool(redis_url)

# In-memory sessions are process-local, so multiple workers will randomly lose
# assemble/run/hardware state across requests in production. Keep memory mode to
# one process and allow wider worker fan-out only when a shared backend exists.
configured_workers = max(1, int(os.getenv("GUNICORN_WORKERS", "2")))
workers = configured_workers if shared_session_backend else 1
worker_class = os.getenv("GUNICORN_WORKER_CLASS", "gthread")
threads = max(1, int(os.getenv("GUNICORN_THREADS", "4")))
worker_connections = max(1, int(os.getenv("GUNICORN_WORKER_CONNECTIONS", "100")))
timeout = max(1, int(os.getenv("GUNICORN_TIMEOUT", "60")))
