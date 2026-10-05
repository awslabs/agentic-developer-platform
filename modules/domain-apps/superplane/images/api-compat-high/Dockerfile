# Preserve the schema-016 application and its reviewed curl repair exactly.
ARG BASE_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-api@sha256:70edd98cb9ed0ec48ec34dade114d6315815194009296f8c54a74fc5be2a748e
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:9cf0a78c494248b3c03adc65658de9e76438e34d8089b417965dfb20d3f42352 AS os-security
FROM ${BASE_IMAGE} AS jose-builder
USER root
ADD --checksum=sha256:fb4eaa44dbeb1c26dcc69e4bd7ec54a1cb8dd64d3b4d81ef08d90ff453f2b01b https://files.pythonhosted.org/packages/c6/77/3a1c9039db7124eb039772b935f2244fbb73fc8ee65b9acf2375da1c07bf/python_jose-3.5.0.tar.gz /tmp/jose.tar.gz
RUN mkdir /tmp/jose && tar -xzf /tmp/jose.tar.gz --strip-components=1 -C /tmp/jose
COPY patch_jose.py /tmp/patch_jose.py
RUN python /tmp/patch_jose.py /tmp/jose && python -m pip wheel --no-deps --wheel-dir=/out /tmp/jose
COPY patch_tarfile.py tarfile-manifest.json /tmp/tarfile-security/
RUN python /tmp/tarfile-security/patch_tarfile.py

FROM ${BASE_IMAGE}
USER root
COPY --from=os-security /packages/ /tmp/adp-packages/
COPY --from=os-security /tools/ /tmp/adp-package-tools/
RUN python /tmp/adp-package-tools/install-packages.py /tmp/adp-packages --python-expat && rm -rf /tmp/adp-packages
COPY --from=os-security /python/3.12.14/ /tmp/adp-python-expat/
RUN python /tmp/adp-package-tools/install-python-expat.py /tmp/adp-python-expat && rm -rf /tmp/adp-python-expat /tmp/adp-package-tools
COPY --from=jose-builder /out/ /tmp/jose-wheels/
RUN python -m pip install --no-cache-dir /tmp/jose-wheels/python_jose-*.whl \
    && python -m pip uninstall -y ecdsa && python -m pip check && rm -rf /tmp/jose-wheels
COPY tarfile-manifest.json /opt/adp-security/api-high/tarfile-manifest.json
RUN python -c "import hashlib,json,tarfile; from pathlib import Path; m=json.loads(Path('/opt/adp-security/api-high/tarfile-manifest.json').read_text()); assert hashlib.sha256(Path(tarfile.__file__).read_bytes()).hexdigest()==m['before']"
COPY --from=jose-builder /usr/local/lib/python3.12/tarfile.py /usr/local/lib/python3.12/tarfile.py
COPY PSF-LICENSE.txt /opt/adp-security/api-high/PSF-LICENSE.txt
RUN rm -f /usr/local/lib/python3.12/__pycache__/tarfile.*.pyc
COPY verify.py /opt/adp-security/api-high/verify.py
RUN python /opt/adp-security/api-high/verify.py
USER 65532:65532
