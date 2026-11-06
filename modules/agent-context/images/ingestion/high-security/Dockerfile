ARG PILLOW_SECURITY_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:7b3df072a13b928bddbf70d71735fdc8572006442048868914627ebc8e635b68
FROM ${PILLOW_SECURITY_IMAGE} AS pillow-security
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-ingestion@sha256:934b09fa0c9d4f52c90c68fa9227ba890ecadb303f8cff01a533fac3062c6c10 AS tools
FROM public.ecr.aws/docker/library/node:22.23.3-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS node
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-zoekt@sha256:29e0ded26418f3f1546a3758a10786f78e822ece44379ccc304598d274d50eed AS zoekt
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-ingestion@sha256:9184b85a5bb2f446797b79639f501a76abe698af57b421944af28759954e4b49
USER root
ADD --checksum=sha256:42d158b4d8f7b61ac0a830567c940a86098fb7aac52e467a5ebec03ef5cc2f8d https://go.dev/dl/go1.26.9.linux-amd64.tar.gz /tmp/go.tar.gz
RUN rm -rf /usr/local/go && tar -xzf /tmp/go.tar.gz -C /usr/local && rm /tmp/go.tar.gz
RUN apt-get update && apt-get install -y --no-install-recommends libicu76 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/LICENSE /usr/share/licenses/node/LICENSE
COPY --from=zoekt /usr/local/bin/zoekt-git-index /usr/local/bin/zoekt-git-index
COPY --from=zoekt /usr/share/licenses/ /usr/share/licenses/zoekt-security/
COPY --from=tools /packages/libxml2_2.13.9-0+adp3_amd64.deb /tmp/
RUN dpkg -i /tmp/libxml2_2.13.9-0+adp3_amd64.deb && rm /tmp/libxml2_2.13.9-0+adp3_amd64.deb && ldconfig
COPY --from=tools /packages/nltk-3.10.3+adp1-py3-none-any.whl /tmp/
RUN python -m pip install --no-cache-dir --no-deps /tmp/nltk-3.10.3+adp1-py3-none-any.whl && rm /tmp/nltk-3.10.3+adp1-py3-none-any.whl && python -m pip check
COPY --from=tools /packages/xml.8.0.4.nupkg /tmp/
ADD --checksum=sha256:bb691c98abc8797eedbd48d3a766bc734214aed746e52e2691d8bbb268c60764 https://builds.dotnet.microsoft.com/dotnet/Sdk/8.0.426/dotnet-sdk-8.0.426-linux-x64.tar.gz /tmp/dotnet-sdk.tar.gz
RUN rm -rf /usr/share/dotnet && mkdir -p /usr/share/dotnet \
    && tar -xzf /tmp/dotnet-sdk.tar.gz -C /usr/share/dotnet && rm /tmp/dotnet-sdk.tar.gz
COPY patch-dotnet.py /tmp/
RUN python /tmp/patch-dotnet.py && rm /tmp/xml.8.0.4.nupkg /tmp/patch-dotnet.py
COPY --from=tools /bin/syft /bin/trivy /bin/osv-scanner /usr/local/bin/
COPY --from=tools /usr/share/licenses/osv-scanner/ /usr/share/licenses/osv-scanner/
COPY --from=tools /bin/scip-go /opt/go/bin/scip-go
COPY --from=tools /bin/tsc /usr/lib/node_modules/@sourcegraph/scip-python/node_modules/@typescript/typescript-linux-x64/lib/tsc
COPY --from=tools /usr/share/licenses/ingestion-security-tools/ /usr/share/licenses/ingestion-go-tools/
ADD --checksum=sha256:42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl /tmp/critical-python/certifi-2026.10.19-py3-none-any.whl
ADD --checksum=sha256:fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70 https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl /tmp/critical-python/pip-26.2.2-py3-none-any.whl
ADD --checksum=sha256:264fd5b200f6f6170cb551a4b9fcaabf8aa846ddaefe72db4d5f49f9d3743df9 https://files.pythonhosted.org/packages/65/85/4c57c3cd2eee42878677b4943086e6b36cefc50214a5447028268045ce63/brotli-1.2.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl /tmp/critical-python/brotli-1.2.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
RUN --network=none python -m pip install --no-index --no-deps --no-cache-dir /tmp/critical-python/*.whl \
    && python -m pip check && rm -rf /tmp/critical-python
# The source-built Pillow 13 wheel also fixes its bundled Brotli decoder.
RUN --network=none --mount=type=bind,from=pillow-security,source=/wheels,target=/pillow-wheels \
    python -m pip install --no-index --no-deps --force-reinstall --no-cache-dir /pillow-wheels/pillow-13.0.0+adp1-cp313-*.whl \
    && python -m pip check
COPY --from=pillow-security /maintenance/pillow/ /opt/adp-security/pillow/
COPY --from=pillow-security /maintenance/pillow-wheel-lock.json /opt/adp-security/pillow/wheel-lock.json
USER appuser
