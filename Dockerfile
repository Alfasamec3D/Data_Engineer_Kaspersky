# syntax=docker/dockerfile:1
# vLLM (CPU): rootless, pinned dependencies, private logging.
# Build:  docker build -t vllm-cpu:0.27.1 .
# requirements.txt must exist before building (./scripts/lock.sh).

ARG UBUNTU_VERSION=24.04

# ---------- builder: install dependencies strictly from requirements.txt ----------
FROM ubuntu:${UBUNTU_VERSION} AS builder
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
# --require-hashes: pip refuses to install anything missing from the lock file or without a hash;
# --no-deps: transitive dependencies are already listed (and pinned) in requirements.txt.
RUN /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.txt \
 && /opt/venv/bin/pip check

# ---------- runtime ----------
FROM ubuntu:${UBUNTU_VERSION}
ENV DEBIAN_FRONTEND=noninteractive
# libtcmalloc and libnuma are recommended by vLLM for the CPU backend.
# g++ and python3-dev are required: the CPU sampler is compiled with torch.inductor at runtime,
# and it fails with "No working C++ compiler found" without them (even with --enforce-eager).
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-dev g++ ca-certificates libtcmalloc-minimal4 libnuma1 tini \
 && rm -rf /var/lib/apt/lists/*

# unprivileged user (uid/gid match securityContext in k8s)
ARG APP_UID=10001
RUN groupadd --gid ${APP_UID} vllm \
 && useradd --uid ${APP_UID} --gid ${APP_UID} --create-home --home-dir /home/vllm --shell /usr/sbin/nologin vllm \
 && mkdir -p /models && chown vllm:vllm /models

COPY --from=builder /opt/venv /opt/venv
COPY privacy_logging/ /opt/vllm-privacy/
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENV PATH=/opt/venv/bin:$PATH \
    HOME=/home/vllm \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/vllm-privacy \
    # custom logging: vLLM reads a dictConfig from this file
    VLLM_CONFIGURE_LOGGING=1 \
    VLLM_LOGGING_CONFIG_PATH=/opt/vllm-privacy/logging_config.json \
    VLLM_NO_USAGE_STATS=1 \
    DO_NOT_TRACK=1 \
    # weights come only from the local volume, no network needed
    HF_HUB_OFFLINE=1 \
    HF_HOME=/home/vllm/.cache/huggingface \
    VLLM_CPU_KVCACHE_SPACE=4

USER 10001:10001
WORKDIR /home/vllm
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]