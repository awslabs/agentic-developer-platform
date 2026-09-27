# DeepWiki image for Agent Context Intelligence Platform
# Based on upstream AsyncFuncAI/deepwiki-open, configured for LiteLLM proxy + Bedrock
#
# Build: docker build -t deepwiki:latest .
# The image uses OPENAI_BASE_URL to route all LLM calls through the LiteLLM proxy.

# Build the exact Debian client source on Trixie; archive authentication uses
# the trusted Debian keyring already present in this immutable base.
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS openssh-builder
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::AllowInsecureRepositories=false update \
    && apt-get -y --no-install-recommends install dpkg-dev git gpgv \
    && apt-get -y --no-install-recommends -Ppkg.openssh.nognome,noudeb build-dep openssh=1:10.0p1-7+deb13u4
WORKDIR /src
RUN apt-get --download-only source openssh=1:10.0p1-7+deb13u4 \
    && mkdir -p /out/source-authentication \
    && cp /var/lib/apt/lists/*InRelease /out/source-authentication/ \
    && sha256sum /var/lib/apt/lists/*Sources* /usr/share/keyrings/debian-archive-keyring.gpg > /out/source-authentication/metadata-sha256.txt \
    && apt-cache showsrc openssh > /out/source-authentication/apt-showsrc.txt
COPY security/openssh/ /build-tools/
RUN python /build-tools/authenticate-source.py && python /build-tools/prepare.py
RUN --network=none sh /build-tools/build.sh

# Official upstream commit d92819a9c9f3b99416e3580ff235fc9d3adf8b89.
# Upstream publishes main/latest/sha tags; the configured v1.2.2 tag is unavailable.
# Official multi-platform Node release; fixes CVE-2026-48930 in the builder and UI runtime.
FROM node:22.23.0-bookworm-slim@sha256:d9f850096136edbc402debdd8729579a288aac64574ada0ff4db26b6ae58b0b2 AS node-release
FROM ghcr.io/asyncfuncai/deepwiki-open@sha256:730b72494f407397c896c586af0a31af9671d509af420e6792d834ff094a9c8f AS node-base
USER root
# Remove NodeSource Node20 instead of leaving a vulnerable second executable.
RUN apt-get purge -y nodejs \
    && rm -rf /usr/lib/node_modules/npm \
    && test ! -e /usr/bin/node && test ! -e /usr/local/bin/node
COPY --from=node-release /usr/local/bin/node /usr/local/bin/node
COPY --from=node-release /usr/local/LICENSE /opt/deepwiki-security/node-LICENSE
RUN ln -s /usr/local/bin/node /usr/bin/node \
    && test "$(node --version)" = v22.23.0
# Next's cold SWC fallback queries the registry through npm. Preserve that path
# with the checksum-pinned fixed package manager, including its dependency tree.
ADD --checksum=sha256:d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483 https://registry.npmjs.org/npm/-/npm-11.20.0.tgz /tmp/npm.tgz
RUN rm -rf /usr/lib/node_modules/npm \
    && mkdir -p /usr/lib/node_modules/npm \
    && tar -xzf /tmp/npm.tgz --strip-components=1 -C /usr/lib/node_modules/npm \
    && rm /tmp/npm.tgz \
    && ln -sf /usr/lib/node_modules/npm/bin/npm-cli.js /usr/bin/npm \
    && ln -sf /usr/lib/node_modules/npm/bin/npx-cli.js /usr/bin/npx \
    && npm --version

FROM node-base AS web-builder
WORKDIR /source
ADD --checksum=sha256:2a6f3d7feba176d7700a0a069b2c6c8a720e06f58ce37ffecdaf8a1093f0abbe https://codeload.github.com/AsyncFuncAI/deepwiki-open/tar.gz/d92819a9c9f3b99416e3580ff235fc9d3adf8b89 /tmp/upstream.tar.gz
COPY security/prepare.py security/source-lock.json security/next-15.5.24.patch /opt/deepwiki-security/
RUN tar -xzf /tmp/upstream.tar.gz --strip-components=1 -C /source \
    && apt-get update && apt-get install -y --no-install-recommends patch \
    && python /opt/deepwiki-security/prepare.py \
    && npm ci --include=dev --legacy-peer-deps --no-audit --no-fund
ENV NEXT_TELEMETRY_DISABLED=1 NODE_OPTIONS=--max-old-space-size=4096
RUN NODE_ENV=production npm run build
# Install locked tar runtime packages with the builder npm; retain metadata and licenses.
COPY security/next-tar/package.json security/next-tar/package-lock.json /opt/next-tar/
RUN cd /opt/next-tar && npm ci --ignore-scripts --no-audit --no-fund
COPY security/install-next-tar.py /opt/deepwiki-security/install-next-tar.py
RUN python /opt/deepwiki-security/install-next-tar.py \
    /source/.next/standalone/node_modules/next /opt/next-tar/node_modules

FROM node-base
# Replace the complete standalone UI built from the retained upstream commit.
RUN rm -rf /app/.next /app/node_modules
COPY --from=web-builder /source/.next/standalone /app/
COPY --from=web-builder /source/.next/static /app/.next/static
COPY --from=web-builder /source/public /app/public
COPY security/source-lock.json /opt/deepwiki-security/source-lock.json

# Apply available vendor security updates while preserving upstream entrypoints.
RUN apt-get update && apt-get -y upgrade \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir GitPython==3.1.59 \
    && python -m pip check
# Install through the package manager so client helpers/configuration and exact
# downstream provenance remain intact; never claim this backport is upstream10.4.
COPY --from=openssh-builder /out/openssh-client_10.0p1-7+deb13u4+adp.security.1_amd64.deb /tmp/openssh-client.deb
RUN dpkg --force-confold -i /tmp/openssh-client.deb \
    && rm /tmp/openssh-client.deb \
    && test "$(dpkg-query -W -f='${Version}' openssh-client)" = '1:10.0p1-7+deb13u4+adp.security.1'
COPY --from=openssh-builder /out/source-lock.json /opt/deepwiki-security/openssh-source-lock.json
# Reviewed CPython3.11 port; helpers/checks are canonical, source hashes are branch-specific.
COPY security/stdlib311/ /opt/adp-stdlib-security/
COPY security-stdlib/apply.py security-stdlib/check.py security-stdlib/PSF-LICENSE.txt /opt/adp-stdlib-security/
RUN apt-get update && apt-get install -y --no-install-recommends patch \
    && python /opt/adp-stdlib-security/apply.py --patch-file cpython-3.11.16.patch \
    && python /opt/adp-stdlib-security/check.py \
    && apt-get purge -y patch && apt-get clean && rm -rf /var/lib/apt/lists/*

# Override default config with our LiteLLM-proxy-aware configs
# These point the "openai" provider at our LiteLLM proxy which routes to Bedrock
COPY config/generator.json /app/api/config/generator.json
COPY config/embedder.json /app/api/config/embedder.json

# Preserve upstream startup/configuration; only cache paths are writable.
RUN addgroup --system --gid 10001 appgroup && \
    adduser --system --uid 10001 --ingroup appgroup --home /home/appuser appuser && \
    install -d -m 0700 -o appuser -g appgroup /home/appuser /home/appuser/.adalflow && \
    install -d -m 0750 -o appuser -g appgroup /app/.next/cache
ENV HOME=/home/appuser \
    PYTHONDONTWRITEBYTECODE=1

# Environment defaults (overridden by K8s manifest)
ENV PORT=8001
ENV NODE_ENV=production
ENV LOG_LEVEL=INFO
ENV LOG_FILE_PATH=/tmp/deepwiki/application.log

EXPOSE 8001 3000

# Drop to non-root before the runtime instructions (USER must precede
# HEALTHCHECK/CMD so the missing-user check is satisfied).
USER appuser

# Health check
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
  CMD curl -sf http://localhost:8001/health || exit 1
