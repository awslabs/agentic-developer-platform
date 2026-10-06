# Context: this jackson-maintenance directory; base must be separately qualified.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN --network=none \
    --mount=type=bind,source=.,target=/maintenance,readonly \
    python3 -B /maintenance/install.py
USER 1000:1000
