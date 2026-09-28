# Maintenance layer over the verified SkyPilot release; application/provider
# versions remain pinned except for the explicitly tested dependencies below.
ARG BASE_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:f45198dd5cf103f72f797a1aeb13bab50d6223f950c59d63e6d671915e7cda5a
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:9cf0a78c494248b3c03adc65658de9e76438e34d8089b417965dfb20d3f42352 AS os-security
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:65e92431d58468d1a662591c0158a0c623e11915304eb3a9ac2a1728037b9164 AS go-security
FROM ${BASE_IMAGE} AS expat-builder
USER root
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY --from=os-security /packages/libexpat1*.deb /tmp/expat-packages/
RUN dpkg -i /tmp/expat-packages/*.deb && rm -rf /tmp/expat-packages
ADD --checksum=sha256:a0da1e72132e950154eca0f6f47d5db828454700de20e5113667940d81e0db04 https://www.python.org/ftp/python/3.10.21/Python-3.10.21.tar.xz /tmp/Python-3.10.21.tar.xz
COPY build_expat310.py /tmp/build_expat310.py
RUN python /tmp/build_expat310.py

FROM ${BASE_IMAGE} AS crypto-builder
USER root
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cargo rustc pkg-config libssl-dev patch && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:e4cfd68c5f3e0bfdad0d38e023239b96a2fe84146481852dffbcca442c245aa5 https://files.pythonhosted.org/packages/47/93/ac8f3d5ff04d54bc814e961a43ae5b0b146154c89c61b47bb07557679b18/cryptography-46.0.7.tar.gz /tmp/cryptography.tar.gz
RUN mkdir -p /src/cryptography && tar -xzf /tmp/cryptography.tar.gz --strip-components=1 -C /src/cryptography
COPY crypto-chain-budget.patch crypto-pkcs7.patch patch_crypto.py /tmp/
RUN python /tmp/patch_crypto.py && python -m pip wheel --no-deps --wheel-dir=/out /src/cryptography
RUN cd /src/cryptography && cargo test --manifest-path src/rust/Cargo.toml -p cryptography-x509-verification


FROM ${BASE_IMAGE} AS vastai-builder
USER root
ADD --checksum=sha256:aae3f9f556b5c42fdf273d4f8b3a8c8988b7a345ab4a005443e6f0746340c68e https://files.pythonhosted.org/packages/source/v/vastai/vastai-1.0.13.tar.gz /tmp/vastai.tar.gz
RUN mkdir /tmp/vastai && tar -xzf /tmp/vastai.tar.gz --strip-components=1 -C /tmp/vastai
COPY patch_vastai.py /tmp/patch_vastai.py
RUN python /tmp/patch_vastai.py /tmp/vastai && python -m pip wheel --no-deps --wheel-dir=/out /tmp/vastai

FROM ${BASE_IMAGE} AS patched-runtime
USER root
COPY --from=os-security /packages/ /tmp/adp-packages/
COPY --from=os-security /tools/ /tmp/adp-package-tools/
RUN python /tmp/adp-package-tools/install-packages.py /tmp/adp-packages --python-expat && rm -rf /tmp/adp-packages
COPY --from=expat-builder /out/ /tmp/expat-adapters/
RUN python /tmp/adp-package-tools/install-python-expat.py /tmp/expat-adapters \
    && cp /tmp/expat-adapters/PSF-LICENSE /opt/adp-debian13-security/PSF-LICENSE \
    && rm -rf /tmp/expat-adapters /tmp/adp-package-tools
COPY --from=go-security /bin/gcloud-crc32c /opt/google-cloud-sdk/bin/gcloud-crc32c
COPY --from=go-security /bin/gke-gcloud-auth-plugin /opt/google-cloud-sdk/bin/gke-gcloud-auth-plugin
COPY --from=go-security /bin/session-manager-plugin /usr/local/sessionmanagerplugin/bin/session-manager-plugin
COPY --from=go-security /usr/share/licenses/skypilot-go-tools/ /usr/share/licenses/skypilot-go-tools/
COPY --from=vastai-builder /out/ /tmp/vastai-wheels/
COPY --from=crypto-builder /out/ /tmp/vastai-wheels/
COPY --from=crypto-builder /crypto-source-manifest.json /opt/adp-security/high-dependencies/crypto-source-manifest.json
# Vast.ai pins Pillow to an affected version. Rebuild the exact same SDK source
# with a local version and tested patch-level dependencies.
RUN python -m pip install --no-cache-dir /tmp/vastai-wheels/*.whl pillow==12.3.0 ray==2.58.0 \
    && python -m pip check && rm -rf /tmp/vastai-wheels
# Ray's runtime-env agent has its own isolated Python dependency directory.
RUN python -m pip install --no-cache-dir --upgrade --target=/usr/local/lib/python3.10/site-packages/ray/_private/runtime_env/agent/thirdparty_files aiohttp==3.14.3 \
    && python -m pip check
ADD --checksum=sha256:18bfbbabb478dfb67f31aeaf428c387f3c3df654582e1309f708ee1f3086830a https://repo.maven.apache.org/maven2/org/apache/httpcomponents/core5/httpcore5/5.4.3/httpcore5-5.4.3.jar /tmp/httpcore5.jar
COPY patch_ray_httpcore.py /tmp/patch_ray_httpcore.py
RUN python /tmp/patch_ray_httpcore.py /usr/local/lib/python3.10/site-packages/ray/jars/ray_dist.jar /tmp/httpcore5.jar \
    && rm /tmp/httpcore5.jar /tmp/patch_ray_httpcore.py
# A stale user-local uv executable shadows the already upgraded Python package.
RUN test -x /usr/local/bin/uv && test -x /usr/local/bin/uvx \
    && ln -sf /usr/local/bin/uv /root/.local/bin/uv \
    && ln -sf /usr/local/bin/uvx /root/.local/bin/uvx \
    && /root/.local/bin/uv --version && /root/.local/bin/uvx --version
COPY patch_tarfile.py /tmp/patch_tarfile.py
RUN python /tmp/patch_tarfile.py && rm /tmp/patch_tarfile.py
COPY verify.py tarfile_regression.py verify_crypto.py /opt/adp-security/high-dependencies/
RUN python /opt/adp-security/high-dependencies/verify.py \
    && python /opt/adp-security/high-dependencies/verify_crypto.py > /opt/adp-security/high-dependencies/crypto-acceptance.json

FROM public.ecr.aws/docker/library/eclipse-temurin:17-jdk@sha256:b64592d40959b4d13b218f6b06b9ab219ff8aa3dad61efd3b5f519ba4d72ef92 AS java-test
COPY --from=patched-runtime /usr/local/lib/python3.10/site-packages/ray/jars/ray_dist.jar /test/ray.jar
COPY --from=vastai-builder /usr/local/lib/python3.10/site-packages/ray/jars/ray_dist.jar /test/baseline.jar
COPY HttpCoreRegression.java /test/
WORKDIR /test
RUN javac -cp ray.jar HttpCoreRegression.java \
    && (java -cp baseline.jar:. HttpCoreRegression > baseline.txt 2>&1; test "$?" -ne 0) \
    && grep -F 'unbounded headers accepted' baseline.txt \
    && java -cp ray.jar:. HttpCoreRegression > httpcore-regression.txt

FROM patched-runtime
COPY --from=java-test /test/httpcore-regression.txt /opt/adp-security/high-dependencies/httpcore-regression.txt
# Match the existing authenticated deployment's enforced runtime identity.
USER 1000:1000
