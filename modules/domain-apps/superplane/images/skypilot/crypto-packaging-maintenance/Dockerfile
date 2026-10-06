# Compile against the same system OpenSSL ABI used by the maintained runtime.
ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS builder
USER root
RUN apt-get update && apt-get install -y --allow-downgrades --no-install-recommends \
    libc6=2.41-12+deb13u4 libc-bin=2.41-12+deb13u4 \
    build-essential cargo rustc pkg-config libssl-dev=3.5.7-1~deb13u3 patch \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir maturin==1.9.4 cffi==2.0.0 setuptools==81.0.0
COPY . /recipe/
ADD --checksum=sha256:e4cfd68c5f3e0bfdad0d38e023239b96a2fe84146481852dffbcca442c245aa5 https://files.pythonhosted.org/packages/47/93/ac8f3d5ff04d54bc814e961a43ae5b0b146154c89c61b47bb07557679b18/cryptography-46.0.7.tar.gz /tmp/cryptography.tar.gz
RUN python -B /recipe/prepare.py /tmp/cryptography.tar.gz /src/cryptography /out
RUN python -m pip wheel --no-deps --no-build-isolation --wheel-dir=/out /src/cryptography \
    && dpkg-query -W > /out/build-packages.tsv \
    && python -m pip freeze > /out/build-python.txt \
    && rustc --version > /out/rustc.txt \
    && cargo --version > /out/cargo.txt \
    && sha256sum /out/*.whl > /out/wheel.sha256
FROM scratch AS wheel
COPY --from=builder /out/ /
