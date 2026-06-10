# syntax=docker/dockerfile:1.7
#
# diarization-service CPU image (skeleton — mock backend).
#
# Build: docker build -t diarization-service:dev .
# Run:   docker run --rm -p 8300:8300 diarization-service:dev

FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DIA_DEVICE=cpu \
    DIA_BACKEND=mock

WORKDIR /app

COPY requirements.txt /app/
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY app /app/app

RUN useradd -u 10001 -m -s /bin/bash dia && chown -R dia:dia /app
USER dia

EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8300/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300", "--workers", "1"]
