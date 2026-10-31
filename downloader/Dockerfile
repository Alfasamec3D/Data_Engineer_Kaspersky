# syntax=docker/dockerfile:1
# Lightweight rootless image for the initContainer: just Python + huggingface_hub.
# Build (from the repo root):  docker build -f downloader/Dockerfile -t vllm-model-downloader:1.0 .
# downloader/requirements.txt must exist before building (./scripts/lock.sh).

FROM python:3.12-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY downloader/requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.txt \
 && /opt/venv/bin/pip check

FROM python:3.12-slim-bookworm
ARG APP_UID=10001
RUN groupadd --gid ${APP_UID} loader \
 && useradd --uid ${APP_UID} --gid ${APP_UID} --create-home --home-dir /home/loader --shell /usr/sbin/nologin loader
COPY --from=builder /opt/venv /opt/venv
COPY downloader/download_model.py /app/download_model.py
ENV PATH=/opt/venv/bin:$PATH \
    HOME=/home/loader \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/home/loader/.cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1
USER 10001:10001
WORKDIR /app
ENTRYPOINT ["python", "/app/download_model.py"]
