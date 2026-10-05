# syntax=docker/dockerfile:1.7
# Dedicated search runtime. Indexing runs in ingestion, which copies the matched
# static zoekt-git-index binary from this image and supplies git and ctags.
FROM public.ecr.aws/docker/library/golang:1.26.8@sha256:6c2a5538f964f1c82f97ad14988bf05de100d922d159d0e398b54c7b0ca0c6c9 AS build
ADD --checksum=sha256:94d67e89e7407a5a2afd8481245ab0b252a43867eff4d818552ef8acd9f0d03d https://codeload.github.com/sourcegraph/zoekt/tar.gz/153817f643cde8b229ee388c1dddbcf07f4798af /tmp/zoekt.tar.gz
WORKDIR /src
RUN tar -xzf /tmp/zoekt.tar.gz --strip-components=1 && rm /tmp/zoekt.tar.gz
# Keep the serving/index format revision fixed; update the vulnerable modules.
COPY go.mod go.sum ./
COPY auth-proxy ./adp-auth-proxy
ENV CGO_ENABLED=0
RUN go mod download && go mod verify
RUN go build -mod=readonly -trimpath \
    -ldflags '-X github.com/sourcegraph/zoekt.Version=153817f643cd-adp-security1' \
    -o /out/ ./cmd/zoekt-webserver ./cmd/zoekt-git-index ./cmd/zoekt
RUN go test ./adp-auth-proxy && go build -trimpath -o /out/zoekt-auth-proxy ./adp-auth-proxy
RUN mkdir -p /rootfs/data/index /rootfs/home/zoekt /rootfs/tmp /rootfs/etc && \
    chmod 1777 /rootfs/tmp && chown -R 100:101 /rootfs/data /rootfs/home/zoekt && \
    printf 'zoekt:x:100:101:Zoekt:/home/zoekt:/sbin/nologin\n' > /rootfs/etc/passwd && \
    printf 'zoekt:x:101:\n' > /rootfs/etc/group && \
    mkdir -p /rootfs/usr/share/licenses/zoekt /rootfs/usr/share/licenses/go /rootfs/usr/share/licenses/go-modules && \
    cp /src/LICENSE /rootfs/usr/share/licenses/zoekt/LICENSE && \
    cp /usr/local/go/LICENSE /rootfs/usr/share/licenses/go/LICENSE && \
    find /go/pkg/mod -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \) \
      -exec cp --parents '{}' /rootfs/usr/share/licenses/go-modules/ \;

FROM scratch
LABEL org.opencontainers.image.source="https://github.com/sourcegraph/zoekt" \
      org.opencontainers.image.revision="153817f643cde8b229ee388c1dddbcf07f4798af" \
      org.opencontainers.image.version="153817f643cd-adp-security1" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /rootfs/ /
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/ /usr/local/bin/
ENV PATH=/usr/local/bin DATA_DIR=/data/index
USER 100:101
WORKDIR /home/zoekt
EXPOSE 6070
CMD ["zoekt-auth-proxy"]
