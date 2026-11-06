# DeepWiki image for Agent Context Intelligence Platform
# Based on upstream AsyncFuncAI/deepwiki-open, configured for LiteLLM proxy + Bedrock
#
# Build: docker build -t deepwiki:latest .
# The image uses OPENAI_BASE_URL to route all LLM calls through the LiteLLM proxy.

# Build the exact Debian client source on Trixie; archive authentication uses
# the trusted Debian keyring already present in this immutable base.
# Tested immutable curl packages and licenses; build input only.
FROM public.ecr.aws/docker/library/python:3.11.17-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS python-security
RUN rm -rf /usr/local/lib/python3.11/site-packages

FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:782f7587ec1a803231eb0fa38518975842d1748df09a6b20f179af1caedf823b AS curl-security

FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS openssh-builder
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::AllowInsecureRepositories=false update \
    && apt-get -y --no-install-recommends install dpkg-dev git gpgv \
    && apt-get -y --no-install-recommends -Ppkg.openssh.nognome,noudeb build-dep openssh=1:10.0p1-7+deb13u4
WORKDIR /src
RUN apt-get --download-only source openssh=1:10.0p1-7+deb13u4 \
    && mkdir -p /out/source-authentication \
    && cp /var/lib/apt/lists/*InRelease /out/source-authentication/ \
    && sha256sum /var/lib/apt/lists/*Sources* /usr/share/keyrings/debian-archive-keyring.gpg > /out/source-authentication/metadata-sha256.txt \
    && apt-cache showsrc openssh > /out/source-authentication/apt-showsrc.txt
COPY security/openssh/ /build-tools/
RUN python /build-tools/authenticate-source.py && python /build-tools/prepare.py
RUN --network=none sh /build-tools/build.sh

# Official upstream commit d92819a9c9f3b99416e3580ff235fc9d3adf8b89.
# Upstream publishes main/latest/sha tags; the configured v1.2.2 tag is unavailable.
# Official multi-platform Node release; fixes CVE-2026-48930 in the builder and UI runtime.
FROM public.ecr.aws/docker/library/node:22.23.3-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS node-release
FROM ghcr.io/asyncfuncai/deepwiki-open@sha256:730b72494f407397c896c586af0a31af9671d509af420e6792d834ff094a9c8f AS node-base
USER root
# Remove NodeSource Node20 instead of leaving a vulnerable second executable.
RUN apt-get purge -y nodejs \
    && rm -rf /usr/lib/node_modules/npm \
    && test ! -e /usr/bin/node && test ! -e /usr/local/bin/node
COPY --from=node-release /usr/local/bin/node /usr/local/bin/node
COPY --from=node-release /usr/local/LICENSE /opt/deepwiki-security/node-LICENSE
RUN ln -s /usr/local/bin/node /usr/bin/node \
    && test "$(node --version)" = v22.23.3
# Next's cold SWC fallback queries the registry through npm. Preserve that path
# with the checksum-pinned fixed package manager, including its dependency tree.
ADD --checksum=sha256:d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483 https://registry.npmjs.org/npm/-/npm-11.20.0.tgz /tmp/npm.tgz
RUN rm -rf /usr/lib/node_modules/npm \
    && mkdir -p /usr/lib/node_modules/npm \
    && tar -xzf /tmp/npm.tgz --strip-components=1 -C /usr/lib/node_modules/npm \
    && rm /tmp/npm.tgz \
    && ln -sf /usr/lib/node_modules/npm/bin/npm-cli.js /usr/bin/npm \
    && ln -sf /usr/lib/node_modules/npm/bin/npx-cli.js /usr/bin/npx \
    && npm --version

FROM node-base AS web-builder
WORKDIR /source
ADD --checksum=sha256:2a6f3d7feba176d7700a0a069b2c6c8a720e06f58ce37ffecdaf8a1093f0abbe https://codeload.github.com/AsyncFuncAI/deepwiki-open/tar.gz/d92819a9c9f3b99416e3580ff235fc9d3adf8b89 /tmp/upstream.tar.gz
COPY security/prepare.py security/source-lock.json security/next-15.5.24.patch /opt/deepwiki-security/
RUN tar -xzf /tmp/upstream.tar.gz --strip-components=1 -C /source \
    && apt-get update && apt-get install -y --no-install-recommends patch \
    && python /opt/deepwiki-security/prepare.py \
    && npm ci --include=dev --legacy-peer-deps --no-audit --no-fund
ENV NEXT_TELEMETRY_DISABLED=1 NODE_OPTIONS=--max-old-space-size=4096
RUN NODE_ENV=production npm run build
# Install locked tar runtime packages with the builder npm; retain metadata and licenses.
COPY security/next-tar/package.json security/next-tar/package-lock.json /opt/next-tar/
RUN cd /opt/next-tar && npm ci --ignore-scripts --no-audit --no-fund
COPY security/install-next-tar.py /opt/deepwiki-security/install-next-tar.py
RUN python /opt/deepwiki-security/install-next-tar.py \
    /source/.next/standalone/node_modules/next /opt/next-tar/node_modules

FROM node-base
# Keep both application and system site-packages on the cp311 ABI.
RUN find /usr/local/lib/python3.11 -mindepth 1 -maxdepth 1 ! -name site-packages -exec rm -rf {} +
COPY --from=python-security /usr/local/bin/python3.11 /usr/local/bin/python3.11
COPY --from=python-security /usr/local/lib/libpython3.11.so.1.0 /usr/local/lib/libpython3.11.so.1.0
COPY --from=python-security /usr/local/lib/libpython3.so /usr/local/lib/libpython3.so
COPY --from=python-security /usr/local/lib/python3.11/ /usr/local/lib/python3.11/
COPY --from=python-security /usr/local/include/python3.11/ /usr/local/include/python3.11/
RUN ln -sf /usr/local/bin/python3.11 /opt/venv/bin/python \
    && ln -sf /usr/local/bin/python3.11 /opt/venv/bin/python3 \
    && ln -sf /usr/local/bin/python3.11 /opt/venv/bin/python3.11 \
    && ln -sf /usr/local/lib/libpython3.11.so.1.0 /opt/venv/lib/libpython3.11.so.1.0 \
    && sed -i 's/^version = .*/version = 3.11.17/' /opt/venv/pyvenv.cfg \
    && ldconfig \
    && python -c 'import sys, ssl, sqlite3, bz2, lzma, ctypes, pyexpat; assert sys.version_info[:3] == (3, 11, 17); assert pyexpat.EXPAT_VERSION == "expat_2.8.5"' \
    && python -m pip check
ENV PYTHON_VERSION=3.11.17 PYTHON_SHA256=bfb74ad39efae27cda510f134ab408e00f9992c56851cfc0b1cdb5646da11599
# Replace the complete standalone UI built from the retained upstream commit.
RUN rm -rf /app/.next /app/node_modules
COPY --from=web-builder /source/.next/standalone /app/
COPY --from=web-builder /source/.next/static /app/.next/static
COPY --from=web-builder /source/public /app/public
COPY security/source-lock.json /opt/deepwiki-security/source-lock.json

# Apply available vendor security updates while preserving upstream entrypoints.
RUN apt-get update && apt-get -y upgrade \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall -y setuptools \
    && python -c "import shutil,sysconfig; from pathlib import Path; shutil.rmtree(Path(sysconfig.get_path('purelib'))/'setuptools', ignore_errors=True)" \
    && python -m pip install --no-cache-dir PyJWT==2.14.0 GitPython==3.1.60 urllib3==2.8.0 setuptools==81.0.0 \
    && python -m pip check
# Refresh the application TLS trust store in its actual virtualenv.
ADD --checksum=sha256:42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl /tmp/critical-python/certifi-2026.10.19-py3-none-any.whl
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/critical-python/pip-26.2.2-py3-none-any.whl
ADD --checksum=sha256:6b3c3d5794212a54434b4856f808f7bdf7ad48eacadaadb5b2346fc9a639365a https://files.pythonhosted.org/packages/71/7e/c1d314406ce47a39180fc66114681d9ac10c2c667e14e4dc1a5e5089f4fb/brotli-1.2.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl /tmp/critical-python/brotli-1.2.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
RUN --network=none /opt/venv/bin/python -m pip install --no-index --no-deps --no-cache-dir /tmp/critical-python/*.whl \
    && /opt/venv/bin/python -m pip check \
    && /usr/local/bin/python3.11 -m pip install --no-index --no-deps --no-cache-dir /tmp/critical-python/pip-26.2.2-py3-none-any.whl \
    && /usr/local/bin/python3.11 -m pip check && rm -rf /tmp/critical-python

# The active application venv and system build tooling are separate installs.
# Patch the system setuptools vendored jaraco.context/wheel without changing /opt/venv.
ADD --checksum=sha256:51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670 https://files.pythonhosted.org/packages/95/9c/c510029fc6ef33a6275cd2c5d3cecd6613dfd6aa401d57c54f1c18852ccf/setuptools-84.0.0-py3-none-any.whl /tmp/setuptools-84.0.0-py3-none-any.whl
RUN /usr/local/bin/python3.11 -m pip install --no-cache-dir --no-deps /tmp/setuptools-84.0.0-py3-none-any.whl \
    && rm /tmp/setuptools-84.0.0-py3-none-any.whl \
    && /usr/local/bin/python3.11 -m pip check

# Install through the package manager so client helpers/configuration and exact
# downstream provenance remain intact; never claim this backport is upstream10.4.
COPY --from=openssh-builder /out/openssh-client_10.0p1-7+deb13u4+adp.security.1_amd64.deb /tmp/openssh-client.deb
RUN dpkg --force-confold -i /tmp/openssh-client.deb \
    && rm /tmp/openssh-client.deb \
    && test "$(dpkg-query -W -f='${Version}' openssh-client)" = '1:10.0p1-7+deb13u4+adp.security.1'
COPY --from=openssh-builder /out/source-lock.json /opt/deepwiki-security/openssh-source-lock.json
# CPython 3.11.17 incorporates the earlier ADP stdlib backports upstream.
# Keep behavioral regressions in the build rather than reapplying an older patch.
COPY security-stdlib/check.py security-stdlib/PSF-LICENSE.txt /opt/adp-stdlib-security/
RUN python /opt/adp-stdlib-security/check.py

COPY --from=curl-security /out/curl_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/curl.deb
COPY --from=curl-security /out/libcurl4t64_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/openssl.deb
COPY --from=curl-security /out/libcurl3t64-gnutls_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/gnutls.deb
RUN dpkg -i /tmp/adp-curl/*.deb && ldconfig && rm -rf /tmp/adp-curl
COPY --from=curl-security /licenses/ /opt/adp-curl-security/

# DeepWiki runs /opt/venv on /usr/local Python 3.11. Debian's unused
# Python 3.13 interpreter is a separate vulnerable installation.
COPY security/remove-unused-system-python.py /opt/deepwiki-security/remove-unused-system-python.py
RUN python /opt/deepwiki-security/remove-unused-system-python.py

# Upstream chat and RAG construct AdalFlow clients without constructor settings.
# Honor OPENAI_BASE_URL for those calls while retaining explicit caller URLs.
COPY proxy_openai.py /app/api/clients/proxy_openai.py
COPY install-proxy-client.py /opt/deepwiki-security/install-proxy-client.py
RUN python /opt/deepwiki-security/install-proxy-client.py
COPY openai_chat.py /app/api/chat/openai_chat.py
COPY install-chat-stream.py /opt/deepwiki-security/install-chat-stream.py
RUN python /opt/deepwiki-security/install-chat-stream.py
COPY github_clone.py /app/api/github_clone.py
COPY install-github-clone.py /opt/deepwiki-security/install-github-clone.py
RUN python /opt/deepwiki-security/install-github-clone.py

# Override default config with our LiteLLM-proxy-aware configs
# These point the "openai" provider at our LiteLLM proxy which routes to Bedrock
COPY config/generator.json /app/api/config/generator.json
COPY config/embedder.json /app/api/config/embedder.json

# Preserve upstream startup/configuration; only cache paths are writable.
RUN addgroup --system --gid 10001 appgroup && \
    adduser --system --uid 10001 --ingroup appgroup --home /home/appuser appuser && \
    install -d -m 0700 -o appuser -g appgroup /home/appuser /home/appuser/.adalflow && \
    install -d -m 0750 -o appuser -g appgroup /app/.next/cache
ENV HOME=/home/appuser \
    PYTHONDONTWRITEBYTECODE=1

# Environment defaults (overridden by K8s manifest)
ENV PORT=8001
ENV NODE_ENV=production
ENV LOG_LEVEL=INFO
ENV LOG_FILE_PATH=/tmp/deepwiki/application.log

EXPOSE 8001 3000

# Drop to non-root before the runtime instructions (USER must precede
# HEALTHCHECK/CMD so the missing-user check is satisfied).
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

USER appuser

# Health check
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=30s \
  CMD curl -sf http://localhost:8001/health || exit 1
