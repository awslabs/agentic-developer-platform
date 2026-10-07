# Build context is this directory after prepare.py has verified all locked artifacts.
ARG BASE_IMAGE=public.ecr.aws/docker/library/python@sha256:2b4f19dae3a777dfc3b76730bda1e82e1f66ab2a2686fa93ca78edbfb4f04ffe
FROM ${BASE_IMAGE}
USER 0
RUN --network=none \
    --mount=type=bind,source=.,target=/maintenance,readonly \
    python3 /maintenance/install.py
