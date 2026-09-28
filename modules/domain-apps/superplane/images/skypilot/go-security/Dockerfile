# syntax=docker/dockerfile:1.7
FROM golang:1.26.8@sha256:6c2a5538f964f1c82f97ad14988bf05de100d922d159d0e398b54c7b0ca0c6c9 AS build
COPY rebuild.sh /recipe/rebuild.sh
COPY gcp-locks/ /recipe/gcp-locks/
RUN /recipe/rebuild.sh /work
# Retain source and dependency notices alongside the replacement executables.
RUN mkdir -p /work/licenses/go /work/licenses/crc32c /work/licenses/ssm /work/licenses/gcp /work/licenses/dependencies && \
    cp /usr/local/go/LICENSE /work/licenses/go/ && \
    cp /work/crc/platform/gcloud-crc32c_licenses/LICENSES.txt /work/licenses/crc32c/ && \
    cp /work/ssm/LICENSE /work/ssm/NOTICE /work/ssm/THIRD-PARTY /work/licenses/ssm/ && \
    cp /work/gcp/LICENSE /work/licenses/gcp/ && \
    find /go/pkg/mod /work/ssm/vendor -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \) \
      -exec cp --parents '{}' /work/licenses/dependencies/ \;
FROM scratch
COPY --from=build /work/bin/ /bin/
COPY --from=build /work/licenses/ /usr/share/licenses/skypilot-go-tools/
USER 65532:65532
