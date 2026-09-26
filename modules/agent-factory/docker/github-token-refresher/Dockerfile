# =============================================================================
# GitHub App Token Refresher
# =============================================================================
# Lightweight container for generating and refreshing GitHub App installation
# tokens. Used as both init container (one-shot) and sidecar (continuous).
#
# Usage:
#   Init container:    SIDECAR_MODE=false  (default, generates token and exits)
#   Sidecar container: SIDECAR_MODE=true   (loops, refreshing before expiry)
# =============================================================================

# Official Amazon Linux 2023.12.20260918.0 multi-platform image.
FROM public.ecr.aws/amazonlinux/amazonlinux@sha256:06da5a3362eda00c5114227ac81abe56dd9b943395553b7c64a310457f4d9e2b

# Install minimal dependencies (baked in, not at runtime).
# curl is omitted: amazonlinux:2023.x ships curl-minimal, which provides curl
# and conflicts with the full curl package. curl-minimal is sufficient here.
RUN dnf upgrade -y \
    && dnf install -y \
      aws-cli \
      openssl \
      python3 \
      shadow-utils \
    && dnf clean all \
    && rm -rf /var/cache/dnf

# Preserve vendor backports while repairing the remaining urllib3 303 body leak.
COPY security/patch-urllib3.py /tmp/patch-urllib3.py
RUN python3 /tmp/patch-urllib3.py && rm /tmp/patch-urllib3.py

# Create non-root user
RUN groupadd -r tokengen && useradd -r -g tokengen -s /sbin/nologin tokengen

# Copy the token generation script
COPY --chmod=755 github-app-token.sh /usr/local/bin/github-app-token.sh

# Create directories for shared volumes
RUN mkdir -p /shared /secrets && chown tokengen:tokengen /shared /secrets

USER tokengen
WORKDIR /shared

# Default environment variables
ENV AWS_REGION=us-east-1 \
    SECRET_GITHUB_APP_ID=deepwiki/github-app-id \
    SECRET_GITHUB_APP_KEY=deepwiki/github-app-key \
    SECRET_GATEWAY_API_KEY=deepwiki/gateway-api-key \
    GITHUB_TOKEN_PATH=/shared/github-token \
    SECRETS_DIR=/secrets \
    GITHUB_TOKEN_REFRESH_INTERVAL=3000 \
    SIDECAR_MODE=false

ENTRYPOINT ["/usr/local/bin/github-app-token.sh"]
