# Supply a reviewed digest produced by platform/arc-runner/Dockerfile.
ARG RUNNER_IMAGE
FROM ${RUNNER_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
USER runner
RUN psql --version && aws --version && terraform version && kubectl version --client
