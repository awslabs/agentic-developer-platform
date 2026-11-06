# Build from the repository root; no source checkout at container startup.
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
WORKDIR /app
RUN apt-get update && apt-get -y upgrade && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY modules/insights/emitter/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY libs/python/adp-common /opt/adp-common
RUN pip install --no-cache-dir --no-deps /opt/adp-common
COPY modules/insights/emitter/server.py /app/server.py
RUN addgroup --system --gid 10001 appgroup && adduser --system --uid 10001 --ingroup appgroup appuser
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/health', timeout=5).close()"
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]
