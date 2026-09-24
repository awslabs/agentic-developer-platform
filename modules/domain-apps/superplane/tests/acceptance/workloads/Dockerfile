# Build context: this directory. Release input must name a reviewed PyTorch CUDA
# image by digest. The resulting image must also be used by its digest in the
# approved batch profile. No build or workload is triggered by this fixture.
ARG PYTORCH_IMAGE
FROM ${PYTORCH_IMAGE}
COPY cuda_probe.py /opt/superplane-demo/cuda_probe.py
USER 65532:65532
ENTRYPOINT ["python", "/opt/superplane-demo/cuda_probe.py"]
