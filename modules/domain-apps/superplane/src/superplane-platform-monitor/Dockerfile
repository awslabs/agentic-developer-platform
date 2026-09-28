# ============================================================================
# Multi-stage Dockerfile for the Superplane Platform Monitor
# ============================================================================

# Stage 1: Build the Go binary
FROM public.ecr.aws/docker/library/golang:1.26-alpine AS builder

RUN apk add --no-cache git ca-certificates

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags="-s -w" -o /superplane-platform-monitor .

# Stage 2: Minimal runtime image
#
# BASE VERSION. Alpine 3.24 (current stable) rather than 3.19, which reached
# end-of-life on 2025-11-01 and therefore no longer receives security updates.
# The stale Alpine base is a source of OS-level findings in the S21 security
# scan (#6124, parent #5599). Pinning a supported release is the fix; the
# major-minor pin is deliberate so a rebuild cannot silently drift onto an
# untested base.
#
# THE RUNTIME PACKAGE SET IS DELIBERATELY SMALL. The platform monitor reaches
# all external dependencies over HTTP (the observation API, per main.go), and
# starts no subprocess: it contains no os/exec import and no exec.Command call
# anywhere. Only ca-certificates (TLS trust store) and tzdata (time zone data
# for log timestamps) are needed.
FROM public.ecr.aws/docker/library/alpine:3.24

RUN apk add --no-cache ca-certificates tzdata

# Non-root user for security. Numeric UID 65532 so Kubernetes runAsNonRoot
# can verify it without resolving /etc/passwd, matching the controller and
# other Superplane images.
RUN adduser -D -u 65532 monitor
# The static monitor needs no runtime package manager. Alpine's apk libraries
# otherwise retain zlib 1.3.2 (CVE-2026-85091) despite no application dependency.
# Keep the trust store, timezone data and non-root identity installed above.
RUN apk del --purge apk-tools libapk zlib
USER 65532:65532

COPY --from=builder /superplane-platform-monitor /usr/local/bin/superplane-platform-monitor

EXPOSE 9090

ENTRYPOINT ["superplane-platform-monitor"]
