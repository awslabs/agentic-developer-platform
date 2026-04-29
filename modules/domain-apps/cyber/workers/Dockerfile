FROM public.ecr.aws/docker/library/python:3.12-slim AS base

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

# YARA rule corpus — Florian Roth's signature-base, pinned to master HEAD.
# Used by Stage 3 static analysis; shallow clone keeps image small.
RUN git clone --depth 1 --branch master https://github.com/Neo23x0/signature-base.git /opt/yara-rules \
    && rm -rf /opt/yara-rules/.git

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY entrypoint.py ./entrypoint.py
COPY triage/ ./triage/
COPY static/ ./static/

# Non-root user (reduces blast radius when static-analysis parsers get fed nasty input)
RUN useradd -m -s /bin/bash -u 1001 worker && chown -R worker:worker /app
USER worker

ENTRYPOINT ["python", "entrypoint.py"]
