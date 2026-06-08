# ---- build stage ----
FROM public.ecr.aws/docker/library/node:22-slim AS build
ENV BUN_INSTALL=/root/.bun
ENV PATH=/root/.bun/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://bun.sh/install | bash
WORKDIR /app
RUN bun install -g github:garrytan/gbrain@v0.42.26.0

# ---- runtime stage ----
FROM public.ecr.aws/docker/library/node:22-slim
ENV BUN_INSTALL=/root/.bun
ENV PATH=/root/.bun/bin:$PATH
WORKDIR /app

# Bun runtime is required to run gbrain — install it in the final image too
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://bun.sh/install | bash

# Copy gbrain from global install
COPY --from=build /root/.bun/install/global/node_modules /app/node_modules
COPY --from=build /root/.bun/bin/gbrain /usr/local/bin/gbrain

# Copy configuration
COPY config/ /app/config/

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:3000/health || exit 1

CMD ["gbrain", "serve", "--http", "--port", "3000"]
