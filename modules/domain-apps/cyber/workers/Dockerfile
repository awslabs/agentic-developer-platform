FROM public.ecr.aws/docker/library/python:3.13-slim AS base

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
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# YARA rules are fetched at runtime via initContainer from S3.
# See: modules/domain-apps/cyber/k8s/cyber-static-scaledjob.yaml
# Issue #272: Removed baked git clone of signature-base (~200 MB savings).
# The YARA_RULES_DIR env var points to the volume-mounted /rules/ directory.

WORKDIR /app

COPY workers/requirements.txt .
RUN pip install -r requirements.txt

COPY workers/entrypoint.py ./entrypoint.py
COPY workers/sample_access.py ./sample_access.py
COPY workers/script_guard.py ./script_guard.py
COPY workers/triage/ ./triage/
COPY workers/static/ ./static/
COPY workers/scripts/ ./scripts/

# Issue #5616: the worker enforces Mode B script validation itself, so the
# validator must be in the image. It is copied from the Stage-3 skill rather
# than vendored here so the agent-side pre-upload check and the worker-side
# enforcement cannot drift into two different rule sets. This is why the build
# context is the cyber module root rather than workers/.
COPY agent/skills/stage-3-static/validate_script.py ./skills/stage-3-static/validate_script.py

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
