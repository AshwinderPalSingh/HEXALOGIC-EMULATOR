FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HEXLOGIC_SESSION_BACKEND=memory \
    HEXLOGIC_MAX_CONTENT_LENGTH=262144 \
    HEXLOGIC_MAX_SOURCE_CHARS=200000 \
    HEXLOGIC_MAX_RUN_STEPS=100000 \
    HEXLOGIC_ENABLE_DEBUG_TRACE=0

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--threads", "4", "--timeout", "60", "wsgi:app"]
