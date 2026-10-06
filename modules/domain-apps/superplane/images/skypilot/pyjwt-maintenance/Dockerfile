# Context: images/skypilot. Preserve the exact reviewed provider/tool/OS artifact.
ARG BASE_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:4c881a130e5e1abd5f70b7e518616bd62648d85c2f2cd919d8395bb9f48cf233
FROM ${BASE_IMAGE}
USER root
# Bind-mounted inputs leave no installer, wheel or test tooling in the image.
RUN --network=none \
    --mount=type=bind,source=pyjwt-maintenance,target=/maintenance,readonly \
    --mount=type=bind,source=requirements-security.txt,target=/requirements-security.txt,readonly \
    python3 /maintenance/install.py
USER 1000:1000
