# Single-stage build — both stages were identical (same base + bun); multi-stage
# added no size benefit and broke the gbrain binary (COPY dereferenced the bin
# symlink, severing cli.ts from its relative imports).
#
# Base image pinned to specific Node 22 LTS patch for reproducibility and
# vulnerability tracking. Bump when a newer 22.x patch ships OS-level fixes.
FROM public.ecr.aws/docker/library/node:22.23.3-slim
ENV BUN_INSTALL=/opt/bun
ENV PATH=/opt/bun/bin:$PATH
WORKDIR /app

# Pin Bun to a specific version for reproducible builds and auditability.
# gbrain v0.57.0.0 requires bun >= 1.3.11; v1.4.2 satisfies that.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/bun \
    && curl -fsSL https://bun.sh/install | bash -s "bun-v1.4.2"

# Install gbrain globally — pinned to commit 467ff6737f (v0.57.0.0 per
# package.json on master, 2026-09-24). Includes the v0.50.5.0 critical+high
# advisory wave (.env quarantine, transcript redaction, consent pins) and the
# v0.50.0.0 secure-consent fixes. Uses #ref syntax; the @ref shorthand is
# mis-parsed by Bun (see #1275). The global install creates a symlink at
# /opt/bun/bin/gbrain → .../node_modules/gbrain/src/cli.ts which bun
# resolves correctly (relative imports work from the symlink target's real path).
RUN bun install -g github:garrytan/gbrain#467ff6737f741375a9a78eafd2818634d908eb99

# Copy configuration and entrypoint
COPY config/ /app/config/
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Non-root user for security
RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser && \
    chown -R appuser:appgroup /app /opt/bun

EXPOSE 3000

# Drop to non-root before the runtime instructions (USER must precede
# HEALTHCHECK/CMD so the missing-user check is satisfied).
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:3000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
