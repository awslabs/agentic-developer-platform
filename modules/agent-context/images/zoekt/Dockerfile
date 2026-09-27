# Preserve the deployed index format and binaries while repairing Alpine TLS
# libraries. The application source revision remains 153817f643cd.
FROM ghcr.io/sourcegraph/zoekt@sha256:e270b43ec60732ef9a0d63e49a2c296fb2d05a79d90b03ad31d4dc5879af09a1
USER root
RUN apk upgrade --no-cache \
    && apk info -v libcrypto3 libssl3 libcurl
USER zoekt
