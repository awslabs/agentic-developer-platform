# ============================================================================
# Multi-stage Dockerfile for the Superplane Controller
# ============================================================================

# Stage 1: Build the Go binary
FROM public.ecr.aws/docker/library/golang:1.26-alpine AS builder

RUN apk add --no-cache git ca-certificates

WORKDIR /workspace

# Cache dependencies
COPY go.mod go.sum ./
RUN go mod download

# Copy source
COPY . .

# Build static binary
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -ldflags="-w -s" \
    -o /workspace/superplane-controller \
    .

# Stage 2: Minimal runtime image
#
# BASE VERSION. Alpine 3.24 (latest stable) rather than 3.20, which reached end of life on
# 2026-04-01 and therefore no longer receives routine security updates. Two of the runtime
# findings in issue #5600 exist only because the base was EOL: c-ares (CVE-2025-31498) is
# fixed in 1.34.5-r0 and 3.20 ships 1.33.1-r0. Pinning a supported release is the fix; the
# major-minor pin is deliberate so a rebuild cannot silently drift onto an untested base.
#
# THE RUNTIME PACKAGE SET IS DELIBERATELY SMALL — see tests/test_controller_runtime_surface.py,
# which fails if this list regains a package removed below.
#
# helm, aws-cli, openssh-client and wireguard-tools were removed because the controller
# CANNOT invoke them: it contains no os/exec import and no exec.Command call anywhere, so it
# never starts a subprocess. Every external dependency is reached over HTTP instead — the
# SkyPilot REST API (skypilot/client.go) and the control-plane API — and the Kubernetes
# probes in deploy/controller.yaml are httpGet, not exec. Cloud-side work (SSM activation,
# WireGuard tunnels, node onboarding) is performed by SkyPilot from parameters this
# controller passes to that API; it is not performed in this container. The four packages
# were therefore unreachable code that only widened the attack surface, and they carried
# six of the eight findings:
#
#   helm     -> CVE-2025-53547 (helm 3.14.3 dependency-resolver symlink code execution) and,
#               via Go modules compiled into /usr/bin/helm, GHSA-v778-237x-gjrc +
#               GHSA-hcg3-q754-cr77 (golang.org/x/crypto v0.17.0) and GHSA-v23v-6jw2-98fq
#               (github.com/docker/docker v24.0.7+incompatible). Those three are helm's
#               vendored dependencies, NOT this controller's — go.mod requires no x/crypto
#               and no docker/docker. Confirmed against helm's own go.mod at tag v3.14.3.
#   aws-cli  -> pulls python3 and the whole py3-* chain, which is the sole source of
#               sqlite-libs (CVE-2025-3277), py3-cryptography (GHSA-r6ph-v2qm-q3c2),
#               py3-certifi (GHSA-248v-346w-9cwc) and the py3-jmespath match.
#
# Removing them is what actually resolves those findings; a version bump would only have
# moved them. Per-advisory analysis: docs/security/runs/2026-09-21/S01-disposition.md.
#
# WHAT REMAINS AND WHY: ca-certificates supplies the trust store for outbound HTTPS (the
# control-plane API URL is operator-supplied and may be https). bash remains available
# for operator diagnostics. The e2e health checks use BusyBox wget, which is already
# in Alpine; they do not require curl. Avoid retaining curl and its zlib dependency
# (CVE-2026-85091) in a static Go service that starts no subprocess.
FROM public.ecr.aws/docker/library/alpine:3.24

RUN apk add --no-cache \
    ca-certificates \
    bash

# Create non-root user
RUN adduser -D -u 65532 controller
# Finish package installation before removing the unused package-manager runtime.
RUN apk del --purge apk-tools libapk zlib
USER 65532:65532

WORKDIR /

COPY --from=builder /workspace/superplane-controller /superplane-controller

# Metrics port
EXPOSE 8080
# Health probe port
EXPOSE 8081

ENTRYPOINT ["/superplane-controller"]
