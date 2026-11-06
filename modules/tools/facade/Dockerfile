FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
WORKDIR /app
# Built from the repository root (codebuild/bs-tools-facade-smoke.yml runs
# `docker build --file modules/tools/facade/Dockerfile .`), so sources are
# addressed from the root, not from this directory.
COPY modules/tools/facade/pyproject.toml ./
COPY modules/tools/pyproject.toml ./shared/pyproject.toml
COPY modules/tools/adp_tools ./shared/adp_tools/
COPY modules/tools/facade/tools_facade/ ./tools_facade/
RUN pip install --no-cache-dir ./shared . && mkdir -p /home/facade && chown 65532:65532 /home/facade
ENV HOME=/home/facade PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
EXPOSE 8080
CMD ["uvicorn", "tools_facade.server:app", "--host", "0.0.0.0", "--port", "8080"]
