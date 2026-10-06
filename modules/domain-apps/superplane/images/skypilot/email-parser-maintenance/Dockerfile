# Context: images/skypilot. Supply the exact reviewed parent for composition.
ARG BASE_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:4c881a130e5e1abd5f70b7e518616bd62648d85c2f2cd919d8395bb9f48cf233
FROM ${BASE_IMAGE}
USER root
RUN --network=none \
    --mount=type=bind,source=email-parser-maintenance,target=/maintenance,readonly \
    PYTHONDONTWRITEBYTECODE=1 python3 -B /maintenance/install.py
USER 1000:1000
