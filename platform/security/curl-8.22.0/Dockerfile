ARG BASE=scratch
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkgconf autoconf automake libtool patchelf gnupg \
    libssl-dev libgnutls28-dev libnghttp2-dev libnghttp3-dev libngtcp2-dev \
    libngtcp2-crypto-gnutls-dev libidn2-dev libpsl-dev libssh2-1-dev librtmp-dev \
    libldap2-dev zlib1g-dev libbrotli-dev libzstd-dev libkrb5-dev \
    curl libcurl4t64 libcurl3t64-gnutls openssh-server stunnel4 locales
ADD --checksum=sha256:f7ef3ae8a22e521f289803fe93543eb64c329b58aa73a9e224dfd915a2a5f4f7 https://curl.se/download/curl-8.22.0.tar.xz /tmp/curl.tar.xz
COPY maintainer.asc curl-8.22.0.tar.xz.asc source-lock.json /provenance/
RUN install -d -m 0700 /tmp/gnupg && \
    gpg --homedir /tmp/gnupg --batch --import /provenance/maintainer.asc && \
    gpg --homedir /tmp/gnupg --batch --status-fd 1 --verify /provenance/curl-8.22.0.tar.xz.asc /tmp/curl.tar.xz > /provenance/signature-status.txt && \
    grep -F '[GNUPG:] VALIDSIG 27EDEAF22F3ABCEB50DB9A125CC908FDB71E12C2 ' /provenance/signature-status.txt && \
    mkdir /src && tar -xJf /tmp/curl.tar.xz --strip-components=1 -C /src
ADD --checksum=sha256:2a34d2484ba17847a5d11965704e9dd0fac4c6d8efc75ffe1ec7de66d8c6b6fb https://github.com/ngtcp2/ngtcp2/releases/download/v1.25.0/ngtcp2-1.25.0.tar.xz /tmp/ngtcp2.tar.xz
ADD --checksum=sha256:aad782c23d3f01bd4bb52c8bac7a553b631ef8115fd1612703df6183449fef19 https://github.com/ngtcp2/nghttp3/releases/download/v1.18.0/nghttp3-1.18.0.tar.xz /tmp/nghttp3.tar.xz
RUN mkdir /src-nghttp3 /src-ngtcp2 && \
    tar -xJf /tmp/nghttp3.tar.xz --strip-components=1 -C /src-nghttp3 && \
    tar -xJf /tmp/ngtcp2.tar.xz --strip-components=1 -C /src-ngtcp2 && \
    cd /src-nghttp3 && ./configure --prefix=/opt/adp-quic --enable-lib-only --enable-static --disable-shared --with-pic && make -j2 && make install && \
    cd /src-ngtcp2 && PKG_CONFIG_PATH=/opt/adp-quic/lib/pkgconfig ./configure --prefix=/opt/adp-quic --enable-lib-only --enable-static --disable-shared --with-pic --with-openssl --with-gnutls && make -j2 && make install
COPY build.sh package.py /build-tools/
RUN /build-tools/build.sh
# Both TLS flavors contain the maintained QUIC libraries statically. Keep their
# licenses and exact source identities with the distributed shared objects.
RUN for package in libcurl4t64 libcurl3t64-gnutls; do \
      cp /src-ngtcp2/COPYING /packages/$package/usr/share/doc/$package/ngtcp2-COPYING; \
      cp /src-nghttp3/COPYING /packages/$package/usr/share/doc/$package/nghttp3-COPYING; \
      dpkg-deb --root-owner-group --build /packages/$package /out/${package}_8.22.0-0+adp1_amd64.deb; \
    done

# An artifact target permits inspecting/exporting the exact Debian packages.
FROM scratch AS packages
COPY --from=build /out/ /out/
COPY --from=build /packages/curl/usr/share/doc/curl/ /licenses/curl/
COPY --from=build /packages/libcurl4t64/usr/share/doc/libcurl4t64/ /licenses/libcurl4t64/
COPY --from=build /packages/libcurl3t64-gnutls/usr/share/doc/libcurl3t64-gnutls/ /licenses/libcurl3t64-gnutls/

# Controlled repair overlay. BASE must be an immutable reviewed amd64 Trixie
# candidate; build-overlay.py selects the base and original non-root identity.
FROM ${BASE} AS runtime
USER root
COPY --from=build /out/curl_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/curl.deb
COPY --from=build /out/libcurl4t64_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/openssl.deb
COPY --from=build /out/libcurl3t64-gnutls_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/gnutls.deb
RUN dpkg -i /tmp/adp-curl/*.deb && ldconfig && rm -rf /tmp/adp-curl
# Some minimal bases exclude /usr/share/doc through dpkg policy. Retain source
# identities and all three upstream licenses independently of that policy.
COPY --from=build /packages/curl/usr/share/doc/curl/ /opt/adp-curl-security/curl/
COPY --from=build /packages/libcurl4t64/usr/share/doc/libcurl4t64/ /opt/adp-curl-security/libcurl4t64/
COPY --from=build /packages/libcurl3t64-gnutls/usr/share/doc/libcurl3t64-gnutls/ /opt/adp-curl-security/libcurl3t64-gnutls/
ARG RUNTIME_USER
USER ${RUNTIME_USER}
