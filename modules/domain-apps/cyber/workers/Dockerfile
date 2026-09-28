# ---------------------------------------------------------------------------
# Stage 1: builder — compile C extensions, then discard build tools.
#
# build-essential, git, and libyara-dev are needed only for pip install
# (yara-python, capstone, lief compile native code). They are NOT copied to
# the runtime stage, which eliminates the gcc, binutils, and git CVE families
# from the production image.  Issue #6123.
# ---------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      git \
      libyara-dev \
      libmagic1 \
      libssl-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY workers/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: runtime — only runtime OS packages + pre-compiled Python site.
# ---------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Runtime-only system packages. build-essential, git and unzip are excluded:
# - build-essential/git: moved to the builder stage (issue #6123)
# - unzip: unused at runtime (binwalk has its own decompression)
RUN apt-get update && apt-get install -y --no-install-recommends \
      file \
      libseccomp2 \
      binutils \
      yara \
      libyara-dev \
      libmagic1 \
      libssl-dev \
      osslsigncode \
      upx-ucl \
      binwalk \
      ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# YARA rules are fetched at runtime via initContainer from S3.
# See: modules/domain-apps/cyber/k8s/cyber-static-scaledjob.yaml
# Issue #272: Removed baked git clone of signature-base (~200 MB savings).
# The YARA_RULES_DIR env var points to the volume-mounted /rules/ directory.

# Copy pre-compiled Python packages from the builder stage.
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app

COPY workers/entrypoint.py ./entrypoint.py
COPY workers/sample_access.py ./sample_access.py
COPY workers/script_guard.py ./script_guard.py
COPY workers/isolation.py workers/analyze.py workers/job_delivery.py ./
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
RUN useradd -m -s /bin/bash -u 61161 worker \
    && chmod -R go-w /app /opt/worker-manifest.json
USER worker

ENTRYPOINT ["python", "entrypoint.py"]

# Exact runtime filesystem/kernel tests, built only by security contract CI.
FROM base AS security-test
USER root
RUN pip install pytest==8.3.5
COPY workers/tests/test_isolation.py /app/tests/test_isolation.py
USER worker
ENTRYPOINT ["python", "-m", "pytest", "-q", "/app/tests/test_isolation.py"]

# The managed CodeBuild host does not expose Landlock. Boot the same exported
# worker filesystem under Debian's supported kernel, entirely inside this job.
FROM public.ecr.aws/docker/library/debian:trixie-slim AS kernel-test-tools
RUN apt-get update && apt-get install -y --no-install-recommends \
      qemu-system-x86 e2fsprogs linux-image-cloud-amd64 initramfs-tools \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY workers/tests/vm_init.py workers/tests/run_image_vm.sh /test-tools/
ENTRYPOINT ["bash", "/test-tools/run_image_vm.sh"]

FROM base AS runtime
