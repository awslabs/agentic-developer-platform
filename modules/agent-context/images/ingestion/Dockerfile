# Unified Ingestion Pipeline — enrichment image
# Runs: ingest-repo.py, ingest-url.py, ingest-doc.py, refresh-repos.py,
#        discover-infra.py, sqs-worker.py, publish-ingestion.py,
#        correlate.py, lint-wiki.py, generate-indexes.py,
#        generate-learning-artifacts.py, generate-curricula.py
#
# Build:
#   docker build -t agent-context/ingestion:latest .
#   docker tag agent-context/ingestion:latest ${ECR_REGISTRY}/agent-context/ingestion:latest
#   docker push ${ECR_REGISTRY}/agent-context/ingestion:latest
# Use the same immutable ADP security build as the search Deployment.
# Override ZOEKT_IMAGE only together with the serving image.
ARG ZOEKT_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-zoekt@sha256:9526ea64a3d05fb90358d739474dd233780b61a3cadb063dd597c14b538d144f
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-ingestion@sha256:f5928de2bc4f007f0e7b7becf502d2f2b750542d08fb3ad5577411b9ad6cf8e2 AS high-security-tools

FROM public.ecr.aws/docker/library/ruby:3.4.11-slim-trixie@sha256:5b53e16f47e05acae56897d0f96b9d03dcb4563276e71c96415bf7777c9ba207 AS ruby-security
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:782f7587ec1a803231eb0fa38518975842d1748df09a6b20f179af1caedf823b AS curl-security
FROM public.ecr.aws/docker/library/node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS node-release
FROM ${ZOEKT_IMAGE} AS zoekt

FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81

# System deps: git for cloning, build tools for tree-sitter native extensions
RUN apt-get update && \
    apt-get -y upgrade && \
    apt-get install -y --no-install-recommends \
        git \
        curl \
        gcc \
        g++ \
        make \
        unzip \
        libicu76 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# SCIP indexer toolchain (Phase 1 + Phase 2 languages)
# ---------------------------------------------------------------------------

# Fixed Node runtime and npm dependency tree (CVE-2026-48930 / GHSA-23hp-3jrh-7fpw).
# The official Node image verifies signed release checksums. Do not install Node20.
COPY --from=node-release /usr/local/bin/node /usr/local/bin/node
COPY --from=node-release /usr/local/LICENSE /opt/node/LICENSE
ADD --checksum=sha256:d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483 https://registry.npmjs.org/npm/-/npm-11.20.0.tgz /tmp/npm.tgz
RUN test ! -e /usr/bin/node && ln -s /usr/local/bin/node /usr/bin/node \
    && mkdir -p /usr/lib/node_modules/npm \
    && tar -xzf /tmp/npm.tgz --strip-components=1 -C /usr/lib/node_modules/npm \
    && rm /tmp/npm.tgz \
    && ln -s /usr/lib/node_modules/npm/bin/npm-cli.js /usr/bin/npm \
    && ln -s /usr/lib/node_modules/npm/bin/npx-cli.js /usr/bin/npx \
    && test "$(node --version)" = v22.23.2 \
    && test "$(npm --version)" = 11.20.0

# SCIP indexers via npm (global install)
# Note: scip-java deferred — not in current corpus (D7 in neptune-deep-graph-design.md)
RUN npm install -g --prefix /usr \
    @sourcegraph/scip-python@0.6.6 \
    @sourcegraph/scip-typescript@0.4.0 \
    && npm cache clean --force

# Go 1.26+ (scip-go v0.2.7 requires go >= 1.25; kept for dep resolution of Go repos)
ARG GO_VERSION=1.26.8
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
    | tar -xz -C /usr/local
ENV GOPATH="/opt/go"
ENV PATH="/usr/local/go/bin:/opt/go/bin:${PATH}"

# scip-go (module at github.com/scip-code/scip-go, binary in cmd/scip-go)
COPY --from=high-security-tools /bin/scip-go /opt/go/bin/scip-go

# Matched static indexer from the hardened search image; git is supplied here.
COPY --from=zoekt /usr/local/bin/zoekt-git-index /usr/local/bin/zoekt-git-index

# Ruby 3.4.11 includes patched default zlib and Net::IMAP libraries.
# Keep Sorbet and Bundler while replacing Debian's affected Ruby 3.3 packages.
COPY --from=ruby-security /usr/local/bin/ /usr/local/bin/
COPY --from=ruby-security /usr/local/lib/ /usr/local/lib/
COPY --from=ruby-security /usr/local/include/ /usr/local/include/
COPY --from=ruby-security /usr/local/share/ /usr/local/share/
RUN apt-get update && apt-get install -y --no-install-recommends libyaml-0-2 libgmp10 \
    && apt-get clean && rm -rf /var/lib/apt/lists/* && ldconfig \
    && ruby -r zlib -r net/imap -e 'abort unless RUBY_VERSION == "3.4.11" && Zlib::VERSION == "3.2.3" && Net::IMAP::VERSION == "0.5.15"' \
    && gem install sorbet-static sorbet --no-document

# .NET SDK (for scip-dotnet)
RUN curl -fsSL https://dot.net/v1/dotnet-install.sh | bash -s -- --version 8.0.425 --install-dir /usr/share/dotnet && \
    ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet
ENV DOTNET_ROOT="/usr/share/dotnet"

# JVM deferred — scip-java not in current corpus (D7 in neptune-deep-graph-design.md)
# When Java support is added, install: default-jdk-headless maven

# Validated Go 1.26.8 builds with fixed transitive modules; source locks and
# reproduction recipes are in high-security/. Keep OSV's independent release.
COPY --from=high-security-tools /bin/syft /bin/trivy /usr/local/bin/
COPY --from=high-security-tools /bin/tsc /usr/lib/node_modules/@sourcegraph/scip-python/node_modules/@typescript/typescript-linux-x64/lib/tsc
COPY --from=high-security-tools /usr/share/licenses/ingestion-security-tools/ /usr/share/licenses/ingestion-security-tools/
COPY --from=zoekt /usr/share/licenses/ /usr/share/licenses/zoekt-security/
ARG OSV_SCANNER_VERSION=2.6.0
RUN curl -sSfL "https://github.com/google/osv-scanner/releases/download/v${OSV_SCANNER_VERSION}/osv-scanner_linux_amd64" \
        -o /usr/local/bin/osv-scanner && chmod +x /usr/local/bin/osv-scanner

# Regenerate the verified legacy SCIP bindings before resolving modern protobuf.
COPY security-build/patch-codegraph-wheel.py /tmp/patch-codegraph-wheel.py
RUN pip download --no-deps --only-binary=:all: --dest /tmp/upstream codegraphcontext==0.6.13 && \
    python /tmp/patch-codegraph-wheel.py \
        /tmp/upstream/codegraphcontext-0.6.13-py3-none-any.whl \
        /tmp/patched/codegraphcontext-0.6.13-py3-none-any.whl

# Python deps
RUN pip install --no-cache-dir \
    /tmp/patched/codegraphcontext-0.6.13-py3-none-any.whl \
    "httpx>=0.27.0" \
    "python-ulid>=2.0.0" \
    python-hcl2 \
    tree-sitter \
    tree-sitter-python \
    tree-sitter-typescript \
    tree-sitter-go \
    requests \
    defusedxml>=0.7.1 \
    crawl4ai \
    boto3 \
    gremlinpython \
    opensearch-py \
    markitdown \
    PyJWT[crypto] \
    pydantic-settings \
    psycopg2-binary \
    alembic \
    sqlalchemy \
    asyncpg \
    protobuf \
    python-json-logger==2.0.7 \
    opentelemetry-api>=1.20.0 \
    opentelemetry-sdk>=1.20.0 \
    opentelemetry-exporter-otlp-proto-grpc>=1.20.0 \
    opentelemetry-sdk-extension-aws>=2.0.0 \
    opentelemetry-propagator-aws-xray>=1.0.0 \
    && pip check && rm -rf /tmp/upstream /tmp/patched /tmp/patch-codegraph-wheel.py

# Download Amazon RDS/Neptune CA bundle for TLS verification (#2224)
RUN curl -fsSL -o /etc/ssl/certs/rds-global-bundle.pem \
        https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    && test -s /etc/ssl/certs/rds-global-bundle.pem

# Install Playwright browsers for crawl4ai JS rendering.
# PLAYWRIGHT_BROWSERS_PATH puts browser binaries at a fixed, root-owned location
# readable (but not writable) by the non-root runtime user (#6038).
ENV PLAYWRIGHT_BROWSERS_PATH="/opt/browsers"
RUN crawl4ai-setup && python -m playwright install chromium

# Copy pipeline scripts (config.py must be first — all scripts import it)
# scope.py + status_callback.py are imported by sqs-worker.py / ingest-repo.py /
# publish-ingestion.py — without them the ScaledJob worker dies at import (#2213).
# registry_reader.py is imported by publish-ingestion.py's --from-registry path
# (publish_from_registry, #2172); without it the daily refresh crashes at Step 1
# with ModuleNotFoundError (#2800).
# url_denylist.py / url_fetch.py / s3_source_guard.py are the SSRF + source
# ownership guards imported by ingest-url.py and ingest-doc.py (#5658). Without
# them those scripts die at import, which is the correct failure mode but a
# needless one — they are listed here and asserted by the smoke check below.
COPY config.py telemetry.py tracing.py metrics.py s3_store.py wiki_store.py code_embedder.py sbom_parser.py db.py stage_tracker.py github_auth.py scope.py status_callback.py registry_reader.py lang_go.py url_denylist.py url_fetch.py browser_fetch.py s3_source_guard.py source_admission.py repo_acl.py /app/
COPY scip_ingester.py scip_indexer.py scip_neptune_csv.py scip_neptune_loader.py /app/
COPY iac_terraform_parser.py iac_neptune_csv.py /app/
COPY scip_proto/ /app/scip_proto/
COPY ingest-repo.py ingest-url.py ingest-doc.py refresh-repos.py \
     discover-infra.py sqs-worker.py publish-ingestion.py \
     correlate.py lint-wiki.py generate-indexes.py \
     generate-learning-artifacts.py generate-curricula.py \
     scan-vulns.py /app/

# Build-time import smoke check: fail the image build (not at 06:00 UTC in prod)
# if a top-level /app module is missing from the COPY list. registry_reader was
# shipped in source but never copied, silently breaking the daily refresh (#2800).
# Only pure-import modules (no env/DB needed at import time) are listed here.
RUN cd /app && python -c "import registry_reader, scope, status_callback, lang_go" \
    && python -c "import url_denylist, url_fetch, browser_fetch, s3_source_guard, source_admission, repo_acl"

# Vulnerability scanner pipeline package (used by scan-vulns.py, #2536)
COPY pipeline/ /app/pipeline/
COPY personal_context/ /app/personal_context/

# GitHub App token minting script (from scripts/github-app-token.py)
COPY github-app-token.py /app/github-app-token.py

# Git credential helper for authenticated cloning (GIT_ASKPASS)
COPY git-credential-helper.sh /app/git-credential-helper.sh
RUN chmod +x /app/git-credential-helper.sh

# Package alembic config + migrations for in-cluster migration Jobs (#1437)
COPY alembic/alembic.ini /app/alembic/alembic.ini
COPY alembic/env.py /app/alembic/env.py
COPY alembic/script.py.mako /app/alembic/script.py.mako
COPY alembic/versions/ /app/alembic/versions/

WORKDIR /app

# ---------------------------------------------------------------------------
# Non-root runtime user (#6038 — S15 ingestion hardening)
# ---------------------------------------------------------------------------
# Matches the UID/GID pattern used by sibling images (context-mcp, deepwiki,
# codegraph-context, litellm-proxy). All image-installed binaries and scripts
# remain root-owned; the runtime user cannot modify them.
#
# Writable locations at runtime (provided by K8s emptyDir mounts per manifest):
#   /tmp          — clone scratch, tool temp files, pip/trivy caches
#   /home/appuser — HOME for Go mod cache, Git config, npm temp, dotfiles
#
# /platform-data is a PVC mounted with fsGroup:10001 in the manifests so files
# are group-writable without blanket chmod.
RUN addgroup --system --gid 10001 appgroup && \
    adduser --system --uid 10001 --ingroup appgroup --home /home/appuser appuser

# Ensure the home directory exists and is owned by appuser (adduser usually
# creates it, but be explicit).
# Upstream multi-stage COPY/tar metadata carries its original numeric owner;
# Trivy's release tar uses UID 10001, which is our runtime user. Normalize these
# two installed binaries explicitly before dropping privilege.
RUN chown 0:0 /usr/local/bin/zoekt-git-index /usr/local/bin/trivy && \
    mkdir -p /home/appuser && chown appuser:appgroup /home/appuser

ENV HOME="/home/appuser" \
    GOMODCACHE="/home/appuser/go/pkg/mod" \
    GOCACHE="/home/appuser/.cache/go-build" \
    XDG_CACHE_HOME="/home/appuser/.cache" \
    NPM_CONFIG_CACHE="/home/appuser/.npm" \
    PYTHONDONTWRITEBYTECODE="1"

# Verify the reviewed Python 3.13.16 source bytes and security behavior.
COPY security-stdlib/ /opt/adp-stdlib-security/
RUN python /opt/adp-stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /opt/adp-stdlib-security/check.py

COPY --from=curl-security /out/curl_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/curl.deb
COPY --from=curl-security /out/libcurl4t64_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/openssl.deb
COPY --from=curl-security /out/libcurl3t64-gnutls_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/gnutls.deb
RUN dpkg -i /tmp/adp-curl/*.deb && ldconfig && rm -rf /tmp/adp-curl
COPY --from=curl-security /licenses/ /opt/adp-curl-security/

# Replace the ABI-2 library used by Chromium's LLVM/Mesa dependencies.
COPY --from=high-security-tools /packages/libxml2_2.13.9-0+adp3_amd64.deb /tmp/libxml2.deb
RUN dpkg -i /tmp/libxml2.deb && ldconfig && rm /tmp/libxml2.deb

# Source backports and the actual .NET assembly, not version-only metadata.
COPY --from=high-security-tools /packages/nltk-3.10.3+adp1-py3-none-any.whl /tmp/
RUN python -m pip install --no-cache-dir --no-deps /tmp/nltk-3.10.3+adp1-py3-none-any.whl \
    && rm /tmp/nltk-3.10.3+adp1-py3-none-any.whl && python -m pip check
COPY --from=high-security-tools /packages/xml.8.0.4.nupkg /tmp/
COPY high-security/patch-dotnet.py /tmp/patch-dotnet.py
RUN python /tmp/patch-dotnet.py && rm /tmp/xml.8.0.4.nupkg /tmp/patch-dotnet.py \
    && HOME=/tmp/adp-dotnet-validation DOTNET_CLI_HOME=/tmp/adp-dotnet-validation XDG_CACHE_HOME=/tmp/adp-dotnet-validation/.cache dotnet format --version \
    && rm -rf /tmp/adp-dotnet-validation

USER appuser

# Default: run refresh (CronJob entrypoint)
CMD ["python", "/app/refresh-repos.py"]
