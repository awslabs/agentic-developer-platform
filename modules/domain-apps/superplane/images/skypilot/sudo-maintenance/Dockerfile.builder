FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
RUN apt-get update && apt-get install -y --no-install-recommends build-essential devscripts debhelper dh-sequence-installnss libpam0g-dev libldap2-dev libsasl2-dev libapparmor-dev libselinux1-dev autoconf bison flex libaudit-dev zlib1g-dev po-debconf pkgconf systemd-dev tzdata ca-certificates
COPY artifacts/source /build/source
WORKDIR /build/source
RUN DEB_BUILD_OPTIONS="nocheck parallel=2" dpkg-buildpackage -b -us -uc
FROM scratch AS artifacts
USER 65532:65532
COPY --from=0 /build/*.deb /build/*.buildinfo /build/*.changes /
