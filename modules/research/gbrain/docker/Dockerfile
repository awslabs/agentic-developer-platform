# Single-stage build — both stages were identical (same base + bun); multi-stage
# added no size benefit and broke the gbrain binary (COPY dereferenced the bin
# symlink, severing cli.ts from its relative imports).
FROM public.ecr.aws/docker/library/node:22-slim
ENV BUN_INSTALL=/root/.bun
ENV PATH=/root/.bun/bin:$PATH
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://bun.sh/install | bash

# Install gbrain globally — pinned to commit 959af1068d (v0.42.36.0 per
# package.json on master). Uses #ref syntax; the @ref shorthand is mis-parsed
# by Bun (see #1275). The global install creates a symlink at
# /root/.bun/bin/gbrain → .../node_modules/gbrain/src/cli.ts which bun
# resolves correctly (relative imports work from the symlink target's real path).
RUN bun install -g github:garrytan/gbrain#959af1068d05f54477c70500fb8387a11f9ccb9c

# Copy configuration and entrypoint
COPY config/ /app/config/
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:3000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
