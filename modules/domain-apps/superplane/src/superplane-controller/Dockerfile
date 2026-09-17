# ============================================================================
# Multi-stage Dockerfile for the Superplane Controller
# ============================================================================

# Stage 1: Build the Go binary
FROM public.ecr.aws/docker/library/golang:1.23-alpine AS builder

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
FROM public.ecr.aws/docker/library/alpine:3.20

RUN apk add --no-cache \
    ca-certificates \
    bash \
    curl \
    openssh-client \
    aws-cli \
    wireguard-tools \
    helm

# Create non-root user
RUN adduser -D -u 65532 controller
USER 65532:65532

WORKDIR /

COPY --from=builder /workspace/superplane-controller /superplane-controller

# Metrics port
EXPOSE 8080
# Health probe port
EXPOSE 8081

ENTRYPOINT ["/superplane-controller"]
