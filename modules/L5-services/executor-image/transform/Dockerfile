ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER 0
COPY worker.whl /tmp/worker.whl
COPY atx.zip /tmp/atx.zip
RUN python -c "import hashlib,zipfile; assert hashlib.sha256(open('/tmp/atx.zip','rb').read()).hexdigest() == '2538a09e24ab5a8b2abeef16fec824600d66b5b215dceccd11e5c4f4318e904e'; zipfile.ZipFile('/tmp/atx.zip').extractall('/opt/transform')" \
    && python -c "import hashlib; assert hashlib.sha256(open('/opt/transform/atx','rb').read()).hexdigest() == '87117a33f1aab02e78e0ed216549edef49918329df55e756bebd84d8f79a10bf'" \
    && ln -s /opt/transform/atx /usr/local/bin/atx \
    && /usr/local/bin/node -e "if (Number(process.versions.node.split('.')[0]) < 22) process.exit(1)" \
    && mv /tmp/worker.whl /tmp/adp_l5_services-0.1.0-py3-none-any.whl \
    && python -m pip install --no-cache-dir /tmp/adp_l5_services-0.1.0-py3-none-any.whl \
    && rm /tmp/adp_l5_services-0.1.0-py3-none-any.whl /tmp/atx.zip \
    && chmod 0555 /usr/local/bin/atx \
    && git --version \
    && mkdir -p /work /input \
    && chown 10000:10000 /work
USER 10000:10000
ENTRYPOINT ["python", "-m", "l5_services.adapters.transform.provider_runner"]
