# syntax=docker/dockerfile:1.7
FROM public.ecr.aws/docker/library/golang:1.26.9@sha256:37cfcca63222ec499bec5e8ad7aad52dbf7529db36d5812b8eae2f0621aa92d6 AS build
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
