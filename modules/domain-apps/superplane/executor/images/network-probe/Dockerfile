# Build context: modules/domain-apps/superplane/executor.
# Supply a reviewed Python 3 base image by immutable digest; no default/latest.
ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
WORKDIR /opt/probe
COPY superplane_executor/__init__.py superplane_executor/network_probe.py superplane_executor/network_workload_probe.py ./superplane_executor/
USER 65532:65532
CMD ["python3", "-m", "superplane_executor.network_workload_probe"]
