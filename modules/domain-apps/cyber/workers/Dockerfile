FROM public.ecr.aws/docker/library/python:3.12.13-slim@sha256:401f6e1a67dad31a1bd78e9ad22d0ee0a3b52154e6bd30e90be696bb6a3d7461 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System tools for triage + static analysis
RUN apt-get update && apt-get install -y --no-install-recommends \
      file \
      binutils \
      yara \
      libyara-dev \
      libmagic1 \
      libssl-dev \
      osslsigncode \
      upx-ucl \
      build-essential \
      git \
      unzip \
      binwalk \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# YARA rules are fetched at runtime via initContainer from S3.
# See: modules/domain-apps/cyber/k8s/cyber-static-scaledjob.yaml
# Issue #272: Removed baked git clone of signature-base (~200 MB savings).
# The YARA_RULES_DIR env var points to the volume-mounted /rules/ directory.

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY entrypoint.py ./entrypoint.py
COPY triage/ ./triage/
COPY static/ ./static/
COPY scripts/ ./scripts/

ARG IMAGE_TAG=dev
ENV IMAGE_TAG=${IMAGE_TAG}

# Generate the manifest at build time — captures the exact installed environment.
# Failure to generate is a build failure (not best-effort).
RUN python3 /app/scripts/generate_manifest.py > /opt/worker-manifest.json \
    && python3 -c "import json; json.load(open('/opt/worker-manifest.json'))"

# Non-root user (reduces blast radius when static-analysis parsers get fed nasty input)
RUN useradd -m -s /bin/bash -u 1001 worker && chown -R worker:worker /app
USER worker

ENTRYPOINT ["python", "entrypoint.py"]
