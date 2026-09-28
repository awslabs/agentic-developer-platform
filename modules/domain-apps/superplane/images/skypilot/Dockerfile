# Official CPython patch release on the same Debian Trixie ABI. Keep SkyPilot's
# provider packages; do not copy the official image's pip/site-packages tree.
FROM public.ecr.aws/docker/library/python:3.10.21-slim@sha256:31dd4d9529d02d7436659061cb7564cd4733fc90e5e152709a942d53382ec8d0 AS python-security
RUN rm -rf /usr/local/lib/python3.10/site-packages

# Preserve the reviewed S03 SkyPilot 0.12.3 + setuptools artifact, then apply
# maintained distro and Python dependency fixes. Do not restart from :latest.
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec

USER root
# Keep kubectl within one minor of EKS 1.35 and replace its vulnerable Go runtime.
ADD --checksum=sha256:3cfeaf80be482b435b0aa214aff6e0b2c312ee23c0ff20810c75517b6004c6eb https://dl.k8s.io/release/v1.35.9/bin/linux/amd64/kubectl /usr/local/bin/kubectl
RUN chmod 0755 /usr/local/bin/kubectl && kubectl version --client=true
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get -y upgrade \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements-security.txt /opt/adp-security/requirements-security.txt
RUN python -m pip install --no-cache-dir --index-url https://pypi.org/simple \
    -r /opt/adp-security/requirements-security.txt \
    && python -m pip check

# Replace the complete old stdlib, including obsolete files and bytecode, while
# retaining ABI-compatible cp310 third-party wheels from the provider environment.
RUN find /usr/local/lib/python3.10 -mindepth 1 -maxdepth 1 ! -name site-packages -exec rm -rf {} +
COPY --from=python-security /usr/local/bin/python3.10 /usr/local/bin/python3.10
COPY --from=python-security /usr/local/lib/libpython3.10.so.1.0 /usr/local/lib/libpython3.10.so.1.0
COPY --from=python-security /usr/local/lib/libpython3.so /usr/local/lib/libpython3.so
COPY --from=python-security /usr/local/lib/python3.10/ /usr/local/lib/python3.10/
COPY --from=python-security /usr/local/include/python3.10/ /usr/local/include/python3.10/
ENV PYTHON_VERSION=3.10.21 \
    PYTHON_SHA256=a0da1e72132e950154eca0f6f47d5db828454700de20e5113667940d81e0db04
RUN python -m pip check && python -c "import sys, ssl, sqlite3, bz2, lzma, ctypes; assert sys.version_info[:3] == (3, 10, 21)"

# Keep the upstream entrypoint/command and API behavior; deployment supplies
# the API process command. Health/compatibility tests must use this final image.
RUN python -c "import sky; import ssl; import OpenSSL; import cryptography; import anyio; assert sky.__version__ == '0.12.3'"
