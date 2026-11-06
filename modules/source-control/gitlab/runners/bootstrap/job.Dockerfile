ARG PYTHON_BASE
FROM ${PYTHON_BASE}

# The helper and job use the same UID on fresh Docker-managed volumes.
RUN install -d -m 0700 -o 10001 -g 10001 /builds /cache
ENV HOME=/tmp
WORKDIR /builds
USER 10001:10001
