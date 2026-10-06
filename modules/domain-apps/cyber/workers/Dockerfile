# Build the maintained Authenticode verifier on the same Debian/Python ABI.
# The fixed release enforces the signing certificate's digitalSignature usage.
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS osslsigncode-security
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake libssl-dev zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN test "$(dpkg --print-architecture)" = amd64 \
    && pip install --no-cache-dir cryptography==50.0.2
WORKDIR /src
ADD --checksum=sha256:1cd8ad26c9ed34e5b96d0a0bef0309437e5e71227a67560da03c33797371ccbd https://codeload.github.com/mtrojnar/osslsigncode/tar.gz/beec94e308d1a1e03ca17b05fe089d93c6303e90 /tmp/osslsigncode.tar.gz
RUN tar -xzf /tmp/osslsigncode.tar.gz --strip-components=1 && rm /tmp/osslsigncode.tar.gz \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr \
    && cmake --build build --parallel 2 \
    && DESTDIR=/package cmake --install build \
    && mkdir -p /package/DEBIAN /package/usr/share/doc/osslsigncode \
    && cp LICENSE.txt /package/usr/share/doc/osslsigncode/copyright
RUN --network=none ctest --test-dir build --output-on-failure --parallel 2
COPY workers/security/osslsigncode-control /package/DEBIAN/control
COPY workers/security/osslsigncode-source.json /package/opt/adp-security/osslsigncode.json
RUN python -c 'import hashlib,json; from pathlib import Path; p=Path("/package/opt/adp-security/osslsigncode.json"); d=json.loads(p.read_text()); d["binary_sha256"]=hashlib.sha256(Path("/package/usr/bin/osslsigncode").read_bytes()).hexdigest(); p.write_text(json.dumps(d,indent=2)+"\n")' \
    && dpkg-deb --build --root-owner-group /package /osslsigncode.deb

# Retain Debian's exact Binwalk application bytes while using the maintained
# Python already supplied by the worker base. Its isolated launcher preserves
# the original dependency visibility; redundant vulnerable Python is not shipped.
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS binwalk-security
RUN apt-get update && apt-get install -y --no-install-recommends \
      binwalk=2.4.3+dfsg1-2+deb13u1 python3-binwalk=2.4.3+dfsg1-2+deb13u1 \
    && rm -rf /var/lib/apt/lists/*
COPY workers/security/package_binwalk.py /tmp/package_binwalk.py
RUN python /tmp/package_binwalk.py && dpkg-deb --build --root-owner-group /package /binwalk.deb

# ---------------------------------------------------------------------------
# Stage 1: builder — compile C extensions, then discard build tools.
#
# build-essential, git, and libyara-dev are needed only for pip install
# (yara-python, capstone, lief compile native code). They are NOT copied to
# the runtime stage, which eliminates the gcc, binutils, and git CVE families
# from the production image.  Issue #6123.
# ---------------------------------------------------------------------------
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS builder

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
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS base

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
      upx-ucl \
      ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install the fixed verifier as a normal package, preserving /usr/bin/osslsigncode.
COPY --from=osslsigncode-security /osslsigncode.deb /tmp/osslsigncode.deb
RUN dpkg -i /tmp/osslsigncode.deb && rm /tmp/osslsigncode.deb \
    && osslsigncode --version

COPY --from=binwalk-security /binwalk.deb /tmp/binwalk.deb
RUN dpkg -i /tmp/binwalk.deb && rm /tmp/binwalk.deb \
    && binwalk --help > /dev/null \
    && python -c 'import ssl, sys; assert sys.version_info[:3] == (3, 13, 16)' \
    && test ! -e /usr/lib/python3.13

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
# Exercise both valid behavior and the two repaired security boundaries on every
# build. Tests are bind-mounted, not shipped in the production filesystem.
RUN --network=none --mount=type=bind,source=workers/security,target=/checks,readonly \
    python /checks/verify_osslsigncode.py && python /checks/verify_runtime.py

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
