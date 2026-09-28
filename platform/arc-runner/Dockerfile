# =============================================================================
# ADP Custom ARC Runner Image
# =============================================================================
# Pre-installs the CLI tools ADP workflows need, so deploy steps don't waste
# 3-5 minutes apt-installing zip/aws/kubectl/terraform on every run.
#
# Adapted from aws-innovate/AISuperPlane's infra/arc-runner/Dockerfile.
#
# Pin the runner release and refresh it before GitHub deprecates the version.
# With ARC updates disabled, a deprecated release exits before accepting jobs.
#
# Build context: this directory (no parent references).
# =============================================================================

# Preserve Terraform 1.14.9 behavior while updating its embedded Go dependencies.
# The reviewed patch fixes the dependency graph; no floating go get runs here.
FROM public.ecr.aws/docker/library/golang:1.26.8-trixie@sha256:bdca99a00bc16590cb1a0bb4e698f5fc5d6a64e4d5eef13d9f18a0ee08e5fa65 AS terraform-build
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/hashicorp/terraform.git \
    && git fetch --depth=1 origin c3c23dd8f1201506dbd21788f482420ee3c1b248 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = c3c23dd8f1201506dbd21788f482420ee3c1b248
COPY terraform-security-dependencies.patch /tmp/terraform-security-dependencies.patch
RUN git apply --check /tmp/terraform-security-dependencies.patch \
    && git apply /tmp/terraform-security-dependencies.patch \
    && sha256sum go.mod go.sum > /tmp/terraform-modules.sha256 \
    && go mod download \
    && go mod verify \
    && go build -mod=readonly -p 2 -buildvcs=false -trimpath \
        -ldflags "-s -w -X github.com/hashicorp/terraform/version.dev=no" \
        -o /out/terraform . \
    && sha256sum -c /tmp/terraform-modules.sha256

# Signed maintained Kaniko release; rebuild only executor with reviewed dependencies.
FROM ghcr.io/osscontainertools/kaniko:v1.28.5@sha256:738807f0e31daf07743f89260a95956dcc6ee4f62553f4f6344c990cabab8675 AS kaniko-release
FROM public.ecr.aws/docker/library/golang:1.26.8-trixie@sha256:bdca99a00bc16590cb1a0bb4e698f5fc5d6a64e4d5eef13d9f18a0ee08e5fa65 AS kaniko-build
ENV GOTOOLCHAIN=local CGO_ENABLED=0
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/osscontainertools/kaniko.git \
    && git fetch --depth=1 origin 9c3dcc112b7a71bed4de3bc8c4ed089ef98cbf0c \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 9c3dcc112b7a71bed4de3bc8c4ed089ef98cbf0c
COPY kaniko-security-dependencies.patch /tmp/kaniko-security-dependencies.patch
RUN git apply --check /tmp/kaniko-security-dependencies.patch \
    && git apply /tmp/kaniko-security-dependencies.patch \
    && sha256sum go.mod go.sum > /tmp/kaniko-modules.sha256 \
    && go mod download \
    && go mod verify \
    && go build -mod=readonly -p 1 -buildvcs=false -trimpath \
        -ldflags "-s -w -X github.com/osscontainertools/kaniko/pkg/version.version=v1.28.5" \
        -o /out/executor ./cmd/executor \
    && sha256sum -c /tmp/kaniko-modules.sha256

# Rebuild bundled tools with fixed Go dependencies, keeping reviewed source pins.
FROM public.ecr.aws/docker/library/golang:1.26.8-trixie@sha256:bdca99a00bc16590cb1a0bb4e698f5fc5d6a64e4d5eef13d9f18a0ee08e5fa65 AS high-tools-build
ENV GOTOOLCHAIN=local GOMAXPROCS=2 CGO_ENABLED=0
RUN apt-get update && apt-get install -y --no-install-recommends libseccomp-dev && rm -rf /var/lib/apt/lists/*
COPY security/high-tools/*.patch /patches/
WORKDIR /src/buildx
RUN git init . && git remote add origin https://github.com/docker/buildx.git \
    && git fetch --depth=1 origin 0b265a9f62db554fa9aba6dd19e1bd5704bc7d8a && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 0b265a9f62db554fa9aba6dd19e1bd5704bc7d8a \
    && git apply --check /patches/buildx.patch && git apply /patches/buildx.patch \
    && sha256sum go.mod go.sum > /tmp/buildx-modules.sha256 \
    && go mod download && go mod verify \
    && go build -mod=readonly -p 1 -trimpath -buildvcs=false \
        -ldflags "-s -w -X github.com/docker/buildx/version.Version=v0.37.1" -o /out/buildx ./cmd/buildx \
    && sha256sum -c /tmp/buildx-modules.sha256
WORKDIR /src/helm
RUN git init . && git remote add origin https://github.com/helm/helm.git \
    && git fetch --depth=1 origin 144ca65f8501953fa8b41cd1d37c7223051c85b7 && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 144ca65f8501953fa8b41cd1d37c7223051c85b7 \
    && git apply --check /patches/helm.patch && git apply /patches/helm.patch \
    && sha256sum go.mod go.sum > /tmp/helm-modules.sha256 \
    && go mod download && go mod verify \
    && go build -mod=readonly -p 1 -trimpath -buildvcs=false \
        -ldflags "-s -w -X helm.sh/helm/v3/internal/version.version=v3.22.0" -o /out/helm ./cmd/helm \
    && sha256sum -c /tmp/helm-modules.sha256
WORKDIR /src/acr
RUN git init . && git remote add origin https://github.com/osscontainertools/docker-credential-acr.git \
    && git fetch --depth=1 origin 726f4811d1f2da429555fce4c1868c1a7be96f19 && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = 726f4811d1f2da429555fce4c1868c1a7be96f19 \
    && git apply --check /patches/acr.patch && git apply /patches/acr.patch \
    && sha256sum go.mod go.sum > /tmp/acr-modules.sha256 \
    && go mod download && go mod verify \
    && go build -mod=readonly -p 1 -trimpath -buildvcs=false \
        -ldflags "-s -w" -o /out/docker-credential-acr-env . \
    && sha256sum -c /tmp/acr-modules.sha256
WORKDIR /src/runc
RUN git init . && git remote add origin https://github.com/opencontainers/runc.git \
    && git fetch --depth=1 origin bb14dabeb7185bb72c8c86735d090dcb20f36587 && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = bb14dabeb7185bb72c8c86735d090dcb20f36587 \
    && git apply --check /patches/runc.patch && git apply /patches/runc.patch \
    && sha256sum go.mod go.sum > /tmp/runc-modules.sha256 \
    && go mod download && go mod verify \
    && CGO_ENABLED=1 go build -mod=readonly -p 1 -trimpath -buildvcs=false -buildmode=pie -tags "seccomp urfave_cli_no_docs netgo osusergo" \
        -ldflags "-s -w -X main.gitCommit=bb14dabeb7185bb72c8c86735d090dcb20f36587 -linkmode external -extldflags -static-pie" -o /out/runc . \
    && sha256sum -c /tmp/runc-modules.sha256

# Exact CPython3.14.7 adapters with statically linked Expat2.8.5.
# Build-only artifact, source and normal XML checks are committed in
# platform/arc-runner/security/high-tools/Dockerfile.python314-expat.
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-arc-runner@sha256:9e6dd602c215e93c5e3cd489d4dbbd070d69ac7944caee65b0e46965bc49b881 AS python314-expat

# AWS CLI's official 2.37.4 bundle embeds vulnerable Python 3.14.6.
# Build the maintained portable-exe source distribution with fixed Python.
# This executable is ADP-built; the AWS release ZIP signature does not cover it.
FROM public.ecr.aws/docker/library/python:3.14.7-slim-trixie@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS awscli-security
ARG TARGETARCH=amd64
RUN test "$TARGETARCH" = amd64 && \
    python -c 'import sys; assert sys.version_info[:3] == (3, 14, 7)' && \
    apt-get update && apt-get install -y --no-install-recommends build-essential binutils && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src/awscli
ADD --checksum=sha256:c4d840d5975cea7f3bb51441c6f602312e9ec9dae9afaf725561a250eee73a90 https://codeload.github.com/aws/aws-cli/tar.gz/d22f2115447d212da4af5fc35e440ec7f960aad4 /tmp/awscli-source.tar.gz
RUN tar -xzf /tmp/awscli-source.tar.gz --strip-components=1 && rm /tmp/awscli-source.tar.gz
# Upstream's download-deps backend enforces its bundled dependency hash locks.
RUN ./configure --with-install-type=portable-exe --with-download-deps --prefix=/opt/awscli-adp && \
    make -j2 && make install && \
    cp LICENSE.txt /opt/awscli-adp/LICENSE.txt && \
    cp -r build/exe/aws/THIRD_PARTY_LICENSES /opt/awscli-adp/THIRD_PARTY_LICENSES
COPY --from=python314-expat /modules/ /opt/awscli-adp/lib/aws-cli/python3.14/lib-dynload/
COPY --from=python314-expat /manifest.json /opt/awscli-adp/python314-expat-manifest.json
COPY --from=python314-expat /licenses/ /opt/awscli-adp/XML-LICENSES/
RUN PYTHONPATH=/opt/awscli-adp/lib/aws-cli/python3.14/lib-dynload python -c 'import pyexpat, _elementtree; import xml.etree.ElementTree as E; assert pyexpat.EXPAT_VERSION == "expat_2.8.5"; assert E.fromstring("<root>valid</root>").text == "valid"'
RUN --network=none /opt/awscli-adp/bin/aws --version | grep -F 'aws-cli/2.37.4 Python/3.14.7'
COPY security/high-tools/awscli-source-build.json /opt/awscli-adp/source-build.json

FROM ghcr.io/actions/actions-runner:2.337.0@sha256:e5496277be5d09bc968b3d64911b74e219ac4a3f2edce956a3ecf9271bea1ef4

ENV DEBIAN_FRONTEND=noninteractive

USER root

# Use authenticated Ubuntu mirrors for both indexes and package downloads.
# Plain HTTP returned a stale package/index pair in CodeBuild (package 404).
RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) \
        -exec sed -i \
        -e 's|http://archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' \
        -e 's|http://security.ubuntu.com/ubuntu|https://security.ubuntu.com/ubuntu|g' {} +

# ── Base utilities ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
        curl \
        wget \
        zip \
        unzip \
        git \
        jq \
        ca-certificates \
        gnupg \
        lsb-release \
        software-properties-common \
        sudo \
        apt-transport-https \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 22.x (pinned — security release 2026-09-23: OpenSSL 3.5.6, ────────
#    zlib use-after-free, URL-parsing crash fix)
ENV NODE_VERSION=22.23.3
RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
        -o /tmp/node.tar.xz \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner \
    && rm /tmp/node.tar.xz \
    && node --version && npm --version

# ── Guard GHSA-73rr-hfp2-rc4v: npm >=10.9.8 bundles diff >=5.2.2 natively.
# The manual override was removed when Node 22.23.3 (npm 10.9.9) absorbed
# the fix. This check fails the build if a future Node bump re-introduces
# a vulnerable diff version.
RUN node -e " \
  const v = require('/usr/local/lib/node_modules/npm/node_modules/diff/package.json').version; \
  const [ma,mi,pa] = v.split('.').map(Number); \
  if (ma < 5 || (ma === 5 && mi < 2) || (ma === 5 && mi === 2 && pa < 2)) { \
    console.error('GHSA-73rr-hfp2-rc4v: bundled diff ' + v + ' < 5.2.2'); process.exit(1); } \
  console.log('diff=' + v + ' (>=5.2.2, GHSA-73rr-hfp2-rc4v clear)');"

# ── Python 3.12 + pip (for CodeBuild log parsing, yaml checks, etc.) ────────
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12-venv \
        python3.12-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.12 - --break-system-packages \
    && python3 --version && pip3 --version

# ── boto3 (required by assume-customer-creds.py for SigV4 signing — EPIC #1107)
# botocore.auth.SigV4Auth signs the platform-pod's call to
# /internal/v1/credential-assume-role on the platform API Gateway. Without this,
# every cross-account deploy fails at Load deployment config with
# "botocore is required for SigV4 auth. Install boto3."
RUN pip3 install --no-cache-dir --break-system-packages boto3==1.43.102 \
    && python3 -c "import boto3, botocore.auth; print(f'boto3={boto3.__version__} botocore={botocore.__version__}')"

# ── AWS CLI v2, portable source build with patched Python ──────────────────
COPY --from=awscli-security /opt/awscli-adp /opt/awscli-adp
RUN rm -rf /usr/local/aws-cli && ln -sf /opt/awscli-adp/bin/aws /usr/local/bin/aws \
    && ln -sf /opt/awscli-adp/bin/aws_completer /usr/local/bin/aws_completer \
    && aws --version | grep -F 'aws-cli/2.37.4 Python/3.14.7'

# ── Terraform (matches project version per session-context: 1.14.x) ─────────
ENV TERRAFORM_VERSION=1.14.9
COPY --from=terraform-build /out/terraform /usr/local/bin/terraform
COPY --from=terraform-build /src/LICENSE /usr/local/share/licenses/terraform/LICENSE
COPY terraform-security-NOTICE /usr/local/share/licenses/terraform/NOTICE
RUN terraform version

# ── kubectl (matches cluster: 1.35.x) ───────────────────────────────────────
ENV KUBECTL_VERSION=1.35.9
RUN curl -fsSL "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
        -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && kubectl version --client

# ── Helm 3, same 3.22 release with fixed crypto dependency ──────────────────
COPY --from=high-tools-build /out/helm /usr/local/bin/helm
COPY --from=high-tools-build /src/helm/LICENSE /usr/local/share/licenses/helm/LICENSE
RUN helm version

# ── gh CLI (latest) ─────────────────────────────────────────────────────────
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && out=$(mktemp) \
    && wget -nv -O "$out" https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && cat "$out" | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* "$out" \
    && gh --version

# ── Docker CLI (ECR login/push; actual builds via CodeBuild or Kaniko) ──────
RUN install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/* \
    && docker --version

# ── Kaniko executor (daemonless container builds; no Docker daemon needed) ──
# Maintained v1.28.5 helpers plus executor rebuilt with patched dependencies.
# Keep the release digest and exact source commit aligned.
COPY --from=kaniko-release /kaniko /kaniko
COPY --from=kaniko-build /out/executor /kaniko/executor
COPY --from=kaniko-build /src/LICENSE /usr/local/share/licenses/kaniko/LICENSE
COPY kaniko-security-NOTICE /usr/local/share/licenses/kaniko/NOTICE
RUN chmod -R 777 /kaniko
ENV PATH="/kaniko:${PATH}"

# Update the inherited Docker daemon/runtime tools without removing build capabilities.
ADD --checksum=sha256:d8db66739d2e28d4933786d73e918d9be643a67fbd835db1bf740d650a259e70 https://download.docker.com/linux/static/stable/x86_64/docker-29.8.1.tgz /tmp/docker.tgz
ADD --checksum=sha256:d65eda6a188aac1006848d8060099be88ba9c4bcddbfd9a23a961194710d0dd4 https://github.com/containerd/containerd/releases/download/v2.4.1/containerd-2.4.1-linux-amd64.tar.gz /tmp/containerd.tgz
RUN tar -xzf /tmp/docker.tgz -C /tmp \
    && install -m 0755 /tmp/docker/dockerd /tmp/docker/docker-proxy /tmp/docker/docker-init /usr/bin/ \
    && mkdir /tmp/containerd && tar -xzf /tmp/containerd.tgz -C /tmp/containerd \
    && install -m 0755 /tmp/containerd/bin/* /usr/bin/
COPY --from=high-tools-build /out/runc /usr/bin/runc
COPY --from=high-tools-build /out/buildx /usr/local/lib/docker/cli-plugins/docker-buildx
COPY --from=high-tools-build /out/docker-credential-acr-env /kaniko/docker-credential-acr-env
COPY --from=high-tools-build /src/buildx/LICENSE /usr/local/share/licenses/buildx/LICENSE
COPY --from=high-tools-build /src/runc/LICENSE /usr/local/share/licenses/runc/LICENSE
COPY --from=high-tools-build /src/acr/LICENSE /usr/local/share/licenses/docker-credential-acr/LICENSE
COPY security/high-tools/source-lock.json security/high-tools/NOTICE /usr/local/share/licenses/adp-tools/
COPY security/high-tools/docker-LICENSE /usr/local/share/licenses/docker/LICENSE
COPY security/high-tools/containerd-LICENSE /usr/local/share/licenses/containerd/LICENSE

# Preserve the runner Node runtimes and install compatible patched npm 11.
COPY security/check-npm-tar.cjs /tmp/check-npm-tar.cjs
RUN curl -fsSL https://registry.npmjs.org/npm/-/npm-11.20.0.tgz -o /tmp/runner-npm.tgz \
    && echo "d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483  /tmp/runner-npm.tgz" | sha256sum -c - \
    && rm -rf /home/runner/externals/node20/lib/node_modules/npm \
    && mkdir -p /home/runner/externals/node20/lib/node_modules/npm \
    && tar -xzf /tmp/runner-npm.tgz -C /home/runner/externals/node20/lib/node_modules/npm --strip-components=1 --no-same-owner \
    && /home/runner/externals/node20/bin/node /home/runner/externals/node20/lib/node_modules/npm/bin/npm-cli.js --version \
    && /home/runner/externals/node20/bin/node /tmp/check-npm-tar.cjs /home/runner/externals/node20/lib/node_modules/npm/node_modules/tar \
    && rm /tmp/runner-npm.tgz
RUN curl -fsSL https://registry.npmjs.org/npm/-/npm-11.20.0.tgz -o /tmp/runner-npm.tgz \
    && echo "d1a92f40e6c407b84c3a00c3cf978a10b24fd42f153c527e2016cef7bb34a483  /tmp/runner-npm.tgz" | sha256sum -c - \
    && rm -rf /home/runner/externals/node24/lib/node_modules/npm \
    && mkdir -p /home/runner/externals/node24/lib/node_modules/npm \
    && tar -xzf /tmp/runner-npm.tgz -C /home/runner/externals/node24/lib/node_modules/npm --strip-components=1 --no-same-owner \
    && /home/runner/externals/node24/bin/node /home/runner/externals/node24/lib/node_modules/npm/bin/npm-cli.js --version \
    && /home/runner/externals/node24/bin/node /tmp/check-npm-tar.cjs /home/runner/externals/node24/lib/node_modules/npm/node_modules/tar \
    && rm /tmp/runner-npm.tgz

# Use the same patched npm release for the interactive Node22 CLI.
RUN rm -rf /usr/local/lib/node_modules/npm \
    && cp -a /home/runner/externals/node24/lib/node_modules/npm /usr/local/lib/node_modules/npm \
    && npm --version \
    && node /tmp/check-npm-tar.cjs /usr/local/lib/node_modules/npm/node_modules/tar

# Runner 2.337 supports this migration flag. Its internal hashFiles path still
# names node20, so compatibility links also run that path with supported Node24.
# Workflows requiring Node20-only behavior must migrate before using this image.
ENV FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true
RUN rm -rf /home/runner/externals/node20 /home/runner/externals/node20_alpine \
    && ln -s node24 /home/runner/externals/node20 \
    && ln -s node24_alpine /home/runner/externals/node20_alpine \
    && /home/runner/externals/node20/bin/node --version | grep '^v24\.'

# ── Cleanup ─────────────────────────────────────────────────────────────────
RUN apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* \
    && rm -rf /root/.cache/pip

# Back to runner user
USER runner

# The ARC base emits diagnostic lines alongside --version on stdout. Require
# a successful command and the exact standalone version line.
# Verify every tool is accessible as the runner user.
RUN runner_version_output=$(/home/runner/bin/Runner.Listener --version) \
    && printf '%s\n' "$runner_version_output" | grep -x '2\.337\.0' \
    && echo "=== ADP ARC Runner Tool Verification ===" \
    && node --version \
    && python3 --version \
    && aws --version \
    && terraform version \
    && kubectl version --client \
    && helm version \
    && gh --version \
    && docker --version \
    && jq --version \
    && zip -v | head -1 \
    && /kaniko/executor version \
    && echo "=== All tools verified ==="
