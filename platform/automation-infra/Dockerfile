# Supply a reviewed digest produced by platform/arc-runner/Dockerfile.
# Security scans obtain it from ADP_SECURITY_RUNNER_IMAGE. Updating the runner
# source alone does not update that separately persisted input; see security/.
ARG RUNNER_IMAGE
FROM ${RUNNER_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client python3-yaml \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
USER runner
RUN psql --version && aws --version && terraform version && kubectl version --client
