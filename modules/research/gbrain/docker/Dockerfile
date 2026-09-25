# Single-stage build — both stages were identical (same base + bun); multi-stage
# added no size benefit and broke the gbrain binary (COPY dereferenced the bin
# symlink, severing cli.ts from its relative imports).
#
# Base image pinned to specific Node 22 LTS patch for reproducibility and
# vulnerability tracking. Bump when a newer 22.x patch ships OS-level fixes.
FROM public.ecr.aws/docker/library/node:22.23.3-slim
ENV HOME=/home/appuser
ENV BUN_INSTALL=/opt/bun
ENV PATH=/opt/bun/bin:$PATH
WORKDIR /app

# Node's bundled npm contains fixable advisory findings. Pin the compatible
# patched release and remove its download cache from the final image.
RUN npm install --global npm@12.1.0 && npm cache clean --force

# Pin Bun to a specific version for reproducible builds and auditability.
# gbrain v0.57.0.0 requires bun >= 1.3.11; v1.4.2 satisfies that.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/bun \
    && curl -fsSL https://bun.sh/install | bash -s "bun-v1.4.2"

# Install the exact upstream project with its lockfile and required driver patch.
# A global dependency install ignores gbrain's patchedDependencies; its Postgres
# writer then fails because cancellation support is missing from the driver.
RUN git init /opt/gbrain \
    && git -C /opt/gbrain remote add origin https://github.com/garrytan/gbrain.git \
    && git -C /opt/gbrain fetch --depth 1 origin 467ff6737f741375a9a78eafd2818634d908eb99 \
    && git -C /opt/gbrain checkout --detach FETCH_HEAD \
    && cd /opt/gbrain \
    && bun install --frozen-lockfile --production \
    && ln -s /opt/gbrain/src/cli.ts /opt/bun/bin/gbrain \
    && bun pm cache rm

# Copy configuration and entrypoint
COPY config/ /app/config/
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Non-root user for security
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup --home /home/appuser appuser && \
    chown -R appuser:appgroup /home/appuser /app /opt/bun

EXPOSE 3000

# Drop to non-root before the runtime instructions (USER must precede
# HEALTHCHECK/CMD so the missing-user check is satisfied).
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:3000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
