# wheel is a named build context with the authenticated build output directory.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN --network=none \
    --mount=type=bind,source=.,target=/recipe,readonly \
    --mount=type=bind,from=wheel,source=.,target=/wheels,readonly \
    mkdir -p /opt/adp-security/crypto-packaging \
    && PYTHONDONTWRITEBYTECODE=1 python -B /recipe/install.py \
       /wheels/cryptography-46.0.7+adp1-cp310-abi3-linux_x86_64.whl \
       /recipe/old-installation.json /opt/adp-security/crypto-packaging/install.json \
    && PYTHONDONTWRITEBYTECODE=1 python -B /recipe/ordinary_check.py \
       > /opt/adp-security/crypto-packaging/ordinary-check.json \
    && PYTHONDONTWRITEBYTECODE=1 python -B -m pip check \
    && cp /wheels/prepared-source.json /wheels/build-packages.tsv /wheels/build-python.txt \
       /wheels/rustc.txt /wheels/cargo.txt /wheels/wheel.sha256 \
       /opt/adp-security/crypto-packaging/
USER 1000:1000
