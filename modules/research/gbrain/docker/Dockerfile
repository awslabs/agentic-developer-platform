# Install gbrain in its runtime stage so the CLI symlink and relative imports
# stay intact. The donor stage supplies only reviewed curl Debian packages.
#
# Base image pinned to specific Node 22 LTS patch for reproducibility and
# vulnerability tracking. Bump when a newer 22.x patch ships OS-level fixes.
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:782f7587ec1a803231eb0fa38518975842d1748df09a6b20f179af1caedf823b AS curl-security
FROM public.ecr.aws/docker/library/node:22.23.3-trixie-slim@sha256:154ba2f4d6fec323d28e4f4bb86bba4677f1223391a1979cf521304e03a98dfa
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

# Patch the bundled trust-address parser without resolving other dependencies.
ADD --checksum=sha256:ffcc8055b78b0852b2889a426ad3a3003b60bc79df704faca194157efa2b2587 https://registry.npmjs.org/proxy-addr/-/proxy-addr-2.0.8.tgz /tmp/proxy-addr-2.0.8.tgz
COPY docker/security/ /tmp/proxy-maintenance/
RUN --network=none node /tmp/proxy-maintenance/maintain-proxy-addr.cjs \
    && node /tmp/proxy-maintenance/verify-proxy-addr.cjs \
    && rm -rf /tmp/proxy-maintenance /tmp/proxy-addr-2.0.8.tgz

# Preserve curl/Git behavior with the shared reviewed curl binaries.
COPY --from=curl-security /out/ /tmp/adp-curl/
RUN dpkg -i /tmp/adp-curl/*.deb && ldconfig && rm -rf /tmp/adp-curl
COPY --from=curl-security /licenses/ /opt/adp-curl-security/

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
