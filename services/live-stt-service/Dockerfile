# live-stt-service — CPU-only PoC image
# GPU variant separate Dockerfile.gpu (Faz 24.2 sonrası karar)
#
# Build:
#   docker build -t live-stt-service:dev services/live-stt-service
# Run:
#   docker run --rm -p 8200:8200 \
#     -e STT_MODEL_NAME=medium -e STT_LANGUAGE=tr \
#     live-stt-service:dev

# --- stage 1: deps ------------------------------------------------------------
FROM python:3.11-slim AS deps

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg required by faster-whisper for audio decode
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# --- stage 2: runtime ---------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# ffmpeg only — minimal runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user (PII boundary discipline)
RUN useradd -u 10001 -m -s /bin/bash stt
WORKDIR /app

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY app /app/app

# Whisper model cache directory (persistent volume mount target)
RUN mkdir -p /home/stt/.cache/huggingface \
    && chown -R stt:stt /home/stt /app

USER stt

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8200/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200", "--workers", "1"]
