FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-ingestion@sha256:f5928de2bc4f007f0e7b7becf502d2f2b750542d08fb3ad5577411b9ad6cf8e2 AS tools
FROM node:22.23.2-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS node
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-zoekt@sha256:9526ea64a3d05fb90358d739474dd233780b61a3cadb063dd597c14b538d144f AS zoekt
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-ingestion@sha256:5c96c50f7bc2ed5d591bfcaba0f97c827c4e2d98cc3f3958c58757e5f5178ae8
USER root
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
COPY patch-dotnet.py /tmp/
RUN python /tmp/patch-dotnet.py && rm /tmp/xml.8.0.4.nupkg /tmp/patch-dotnet.py
COPY --from=tools /bin/syft /bin/trivy /usr/local/bin/
COPY --from=tools /bin/scip-go /opt/go/bin/scip-go
COPY --from=tools /bin/tsc /usr/lib/node_modules/@sourcegraph/scip-python/node_modules/@typescript/typescript-linux-x64/lib/tsc
COPY --from=tools /usr/share/licenses/ingestion-security-tools/ /usr/share/licenses/ingestion-go-tools/
USER appuser
