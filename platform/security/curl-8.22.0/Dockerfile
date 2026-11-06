# Standalone scans use the existing reviewed DeepWiki candidate. Component
# overlay builds still pass their explicit BASE and original RUNTIME_USER.
ARG BASE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:c7cf12e4f48eb63a1eb2f86fccc27bec8d7c92f89f0df258ea54f4806ec6b3eb
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
ADD --checksum=sha256:42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl /tmp/critical-python/certifi-2026.10.19-py3-none-any.whl
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/critical-python/pip-26.2.2-py3-none-any.whl
ADD --checksum=sha256:f760d9a9c441bfc87eee720ddb0946d3e19995b20983ae14038e033f1664efcb https://files.pythonhosted.org/packages/aa/f6/8965fda7552aaa285804b9e07c6eac7c475d25fd1de8696103bf8e192bfb/brotli-1.2.1-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl /tmp/critical-python/brotli-1.2.1-cp310-cp310-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
ADD --checksum=sha256:6b3c3d5794212a54434b4856f808f7bdf7ad48eacadaadb5b2346fc9a639365a https://files.pythonhosted.org/packages/71/7e/c1d314406ce47a39180fc66114681d9ac10c2c667e14e4dc1a5e5089f4fb/brotli-1.2.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl /tmp/critical-python/brotli-1.2.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
ADD --checksum=sha256:264fd5b200f6f6170cb551a4b9fcaabf8aa846ddaefe72db4d5f49f9d3743df9 https://files.pythonhosted.org/packages/65/85/4c57c3cd2eee42878677b4943086e6b36cefc50214a5447028268045ce63/brotli-1.2.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl /tmp/critical-python/brotli-1.2.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
RUN --network=none set -e; \
    for interpreter in /opt/venv/bin/python /usr/local/bin/python3; do \
      if [ -x "$interpreter" ] && "$interpreter" -c 'import pip' >/dev/null 2>&1; then \
        "$interpreter" -m pip install --no-index --no-deps --no-cache-dir /tmp/critical-python/pip-26.2.2-py3-none-any.whl; \
        if "$interpreter" -c 'import certifi' >/dev/null 2>&1; then \
          "$interpreter" -m pip install --no-index --no-deps --no-cache-dir /tmp/critical-python/certifi-2026.10.19-py3-none-any.whl; \
        fi; \
        if "$interpreter" -c 'import brotli' >/dev/null 2>&1; then \
          "$interpreter" -m pip install --no-index --no-deps --no-cache-dir --find-links=/tmp/critical-python Brotli==1.2.1; \
        fi; \
        "$interpreter" -m pip check; \
      fi; \
    done; rm -rf /tmp/critical-python
# GHSA-2464-fhr4-w9xw: upstream-supported sharp fix for Next image optimization.
ADD --checksum=sha256:3b6bbbe6b308f5c383938ca4a31926cd5d79c4651ed8c5fda16a97da2014a9ac https://registry.npmjs.org/sharp/-/sharp-0.35.5.tgz /tmp/critical-sharp/sharp-0.35.5.tgz
ADD --checksum=sha256:556787285d3244c07686a355de5b5a4c4d0019a12785bf24764e756a96ea3334 https://registry.npmjs.org/@img/sharp-linux-x64/-/sharp-linux-x64-0.35.5.tgz /tmp/critical-sharp/sharp-linux-x64-0.35.5.tgz
ADD --checksum=sha256:e371cb713fc8822c96a0d3fb60a3d092799d0aefab6d7f1fb3b72d8d75f1d109 https://registry.npmjs.org/@img/sharp-libvips-linux-x64/-/sharp-libvips-linux-x64-1.3.4.tgz /tmp/critical-sharp/sharp-libvips-linux-x64-1.3.4.tgz
RUN --network=none set -e; \
    if [ -d /app/node_modules/next ]; then \
      node -e 'const fs=require("fs"),semver=require("/app/node_modules/semver"); const p=JSON.parse(fs.readFileSync("/app/node_modules/sharp/package.json")); if(!["0.35.4","0.35.5"].includes(p.version)) throw Error("Unreviewed sharp input"); for(const [n,v] of Object.entries({semver:"^7.8.5","@img/colour":"^1.1.0","detect-libc":"^2.1.2"})) if(!semver.satisfies(JSON.parse(fs.readFileSync("/app/node_modules/"+n+"/package.json")).version,v)) throw Error("Incompatible sharp dependency: "+n);'; \
      rm -rf /app/node_modules/sharp; mkdir -p /app/node_modules/sharp; \
      tar -xzf /tmp/critical-sharp/sharp-0.35.5.tgz --strip-components=1 -C /app/node_modules/sharp; \
      rm -rf /app/node_modules/@img/sharp-linux-x64; mkdir -p /app/node_modules/@img/sharp-linux-x64; \
      tar -xzf /tmp/critical-sharp/sharp-linux-x64-0.35.5.tgz --strip-components=1 -C /app/node_modules/@img/sharp-linux-x64; \
      rm -rf /app/node_modules/@img/sharp-libvips-linux-x64; mkdir -p /app/node_modules/@img/sharp-libvips-linux-x64; \
      tar -xzf /tmp/critical-sharp/sharp-libvips-linux-x64-1.3.4.tgz --strip-components=1 -C /app/node_modules/@img/sharp-libvips-linux-x64; \
      node -e 'const {createRequire}=require("module");const sharp=createRequire("/app/node_modules/next/package.json")("sharp");if(sharp.versions.sharp!=="0.35.5") throw Error("Next resolves an affected sharp");'; \
    fi; rm -rf /tmp/critical-sharp
ARG RUNTIME_USER=appuser
USER ${RUNTIME_USER}
