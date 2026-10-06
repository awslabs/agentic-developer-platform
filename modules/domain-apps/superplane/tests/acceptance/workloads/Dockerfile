# Build context: this directory. Release input must name a reviewed PyTorch CUDA
# image by digest. The resulting image must also be used by its digest in the
# approved batch profile. No build or workload is triggered by this fixture.
ARG PYTORCH_IMAGE
FROM ${PYTORCH_IMAGE}
USER root
# Keep the accepted PyTorch/CUDA image and replace only its affected OpenSSL 3.5.
ADD --checksum=sha256:500f505ad9814eba4bd787c87510c1b90f2873f8f3bdb1bdf6200c20742aa987 https://conda.anaconda.org/conda-forge/linux-64/openssl-3.5.8-h781a0a9_0.conda /tmp/openssl-3.5.8-h781a0a9_0.conda
COPY maintain_openssl.py /tmp/maintain_openssl.py
RUN --network=none python /tmp/maintain_openssl.py /tmp/openssl-3.5.8-h781a0a9_0.conda \
    && rm /tmp/maintain_openssl.py /tmp/openssl-3.5.8-h781a0a9_0.conda
COPY cuda_probe.py /opt/superplane-demo/cuda_probe.py
USER 65532:65532
ENTRYPOINT ["python", "/opt/superplane-demo/cuda_probe.py"]
