FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/adapter-venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /build/tools/adapters
COPY modules/tools/pyproject.toml /build/tools/pyproject.toml
COPY modules/tools/adp_tools/ /build/tools/adp_tools/
COPY modules/tools/adapters/pyproject.toml modules/tools/adapters/uv.lock ./
COPY modules/tools/adapters/tool_adapters/ ./tool_adapters/
RUN uv sync --frozen --no-dev --no-editable --no-cache

FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
ARG ADP_SOURCE_REVISION
RUN python -c 'import re,sys; assert re.fullmatch("[0-9a-f]{40}", sys.argv[1])' "$ADP_SOURCE_REVISION"
LABEL org.opencontainers.image.title="adp-tool-adapters" \
      org.opencontainers.image.source="https://github.com/aws-e/adp" \
      org.opencontainers.image.revision="$ADP_SOURCE_REVISION"
COPY --from=build /opt/adapter-venv /opt/adapter-venv
ENV PATH=/opt/adapter-venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ADP_ADAPTER_SOURCE_REVISION=$ADP_SOURCE_REVISION
WORKDIR /opt/adapter-venv
USER 65532:65532
EXPOSE 8080
CMD ["uvicorn", "tool_adapters.server:application", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-graceful-shutdown", "20"]
