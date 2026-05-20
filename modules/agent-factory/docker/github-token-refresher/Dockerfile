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

FROM public.ecr.aws/amazonlinux/amazonlinux:2023

# Install minimal dependencies (baked in, not at runtime).
# curl is omitted: amazonlinux:2023 ships curl-minimal, which provides curl
# and conflicts with the full curl package. curl-minimal is sufficient here.
RUN dnf upgrade -y \
    && dnf install -y \
      aws-cli \
      openssl \
      python3 \
      shadow-utils \
    && dnf clean all \
    && rm -rf /var/cache/dnf

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
