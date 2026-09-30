FROM 000000000101.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:782f7587ec1a803231eb0fa38518975842d1748df09a6b20f179af1caedf823b AS curl-security
FROM 000000000101.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-api@sha256:9e194dfb5ac7189c304ce618597983c95eb487018a5bd3dd3e65fb1c2bf01466
USER root
COPY --from=curl-security /out/curl_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/curl.deb
COPY --from=curl-security /out/libcurl4t64_8.22.0-0+adp1_amd64.deb /tmp/adp-curl/openssl.deb
RUN dpkg -i /tmp/adp-curl/*.deb && ldconfig && rm -rf /tmp/adp-curl
COPY --from=curl-security /licenses/ /opt/adp-curl-security/
USER 65532:65532
