# syntax=docker/dockerfile:1.7
#
# meeting-ai-service CPU image (skeleton — mock backend).
#
# Build: docker build -t meeting-ai-service:dev .
# Run:   docker run --rm -p 8400:8400 meeting-ai-service:dev

FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    MAI_BACKEND=mock

WORKDIR /app

COPY requirements.txt /app/
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY app /app/app

RUN useradd -u 10001 -m -s /bin/bash mai && chown -R mai:mai /app
USER mai

EXPOSE 8400

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8400/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8400", "--workers", "1"]
