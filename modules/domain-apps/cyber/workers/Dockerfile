FROM python:3.12-slim AS base

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
      build-essential \
      git \
      unzip \
      binwalk \
      ssdeep \
      libfuzzy-dev \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
