# Rebuild all upstream controller/listener tools with a fixed Go runtime and
# exact compatible module versions, preserving the upstream runtime image.
FROM public.ecr.aws/docker/library/golang:1.26-alpine@sha256:8ac98ca534ac3f51e1f420a1dd2c15e74c75cfa0f23f3ad27eb5d7236c349a0c AS builder
WORKDIR /src
ADD --checksum=sha256:aa216496c857e23849251eb53f0968e57a16336e48c563ba0bf8887d6d6a942d https://codeload.github.com/actions/actions-runner-controller/tar.gz/9bb16ae49d0ce585d8e682aa7e2668a6e832d5d8 /tmp/source.tar.gz
RUN tar -xzf /tmp/source.tar.gz --strip-components=1 && rm /tmp/source.tar.gz
COPY go.mod go.sum ./
ENV CGO_ENABLED=0 GOMAXPROCS=2
RUN go mod download && go mod verify
RUN --network=none set -eu; \
    flags="-s -w -X github.com/actions/actions-runner-controller/build.Version=0.14.2-adp.security.1 -X github.com/actions/actions-runner-controller/build.CommitSHA=9bb16ae49d0ce585d8e682aa7e2668a6e832d5d8"; \
    go build -p 2 -trimpath -ldflags="$flags" -o /out/manager main.go; \
    go build -p 2 -trimpath -ldflags="$flags" -o /out/ghalistener ./cmd/ghalistener; \
    go build -p 2 -trimpath -ldflags="-s -w" -o /out/github-webhook-server ./cmd/githubwebhookserver; \
    go build -p 2 -trimpath -ldflags="-s -w" -o /out/actions-metrics-server ./cmd/actionsmetricsserver; \
    go build -p 2 -trimpath -ldflags="-s -w" -o /out/sleep ./cmd/sleep

FROM ghcr.io/actions/gha-runner-scale-set-controller@sha256:1b4c7f62e971ab259a4b8798e48e2adaad4af747f45990f474ea5feefa03531d
COPY --from=builder /out/manager /manager
COPY --from=builder /out/ghalistener /ghalistener
COPY --from=builder /out/github-webhook-server /github-webhook-server
COPY --from=builder /out/actions-metrics-server /actions-metrics-server
COPY --from=builder /out/sleep /sleep
COPY source-lock.json /opt/adp-security/arc-source-lock.json
USER 65532:65532
