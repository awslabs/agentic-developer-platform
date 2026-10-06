ARG BASE_IMAGE=sudo-base
FROM ${BASE_IMAGE}
USER root
RUN --network=none \
    --mount=type=bind,source=.,target=/maintenance,readonly \
    python3 /maintenance/install.py
USER 1000:1000
