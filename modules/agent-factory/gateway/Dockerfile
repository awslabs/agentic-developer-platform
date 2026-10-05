# Agent Gateway — SQS Consumer (Pure Python)
# Uses Claude via Bedrock directly — no TypeScript, no subprocess.
# Build context: modules/agent-factory (shared personas are baked in).
# docker build -f gateway/Dockerfile .
# First run: bash modules/agent-factory/scripts/stage-security-bundles.sh

# Reuse authenticated curl packages and the tested portable AWS CLI build.
ARG CURL_SECURITY_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:782f7587ec1a803231eb0fa38518975842d1748df09a6b20f179af1caedf823b
ARG AWSCLI_SECURITY_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime@sha256:cb081f259e95108195c22ef7506eeed843fef6d015f7ce33366ad4f69fd8284c
FROM ${CURL_SECURITY_IMAGE} AS curl-security
FROM ${AWSCLI_SECURITY_IMAGE} AS awscli-security

FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS stdlib-security
COPY security/stdlib/ /opt/stdlib-security/
RUN python /opt/stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /opt/stdlib-security/check.py

FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
COPY --from=stdlib-security /usr/local/lib/python3.13/stringprep.py /usr/local/lib/python3.13/stringprep.py
COPY --from=stdlib-security /usr/local/lib/python3.13/tarfile.py /usr/local/lib/python3.13/tarfile.py
COPY --from=stdlib-security /usr/local/lib/python3.13/urllib/request.py /usr/local/lib/python3.13/urllib/request.py
COPY security/stdlib/ /opt/stdlib-security/
RUN python /opt/stdlib-security/apply.py --verify-only --manifest-file manifest-3.13.16.json \
    && python /opt/stdlib-security/check.py

RUN apt-get update && apt-get -y upgrade && apt-get install -y --no-install-recommends \
    dumb-init curl git bash unzip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=awscli-security /opt/awscli-adp /opt/awscli-adp
RUN ln -s /opt/awscli-adp/bin/aws /usr/local/bin/aws && aws --version

# Keep the reviewed curl packages after distribution package installation.
COPY --from=curl-security /out/ /tmp/adp-curl/
RUN dpkg -i /tmp/adp-curl/*.deb && ldconfig && rm -rf /tmp/adp-curl
COPY --from=curl-security /licenses/ /opt/adp-curl-security/

RUN groupadd -g 10001 agentgw && useradd -u 10001 -g agentgw -s /bin/sh agentgw

WORKDIR /app

COPY gateway/app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir -r /app/app/requirements.txt

COPY gateway/app/ /app/app/
COPY rules/personas/ /app/personas/
COPY gateway/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && chown -R agentgw:agentgw /app

USER agentgw:agentgw

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["dumb-init", "--"]
CMD ["/app/entrypoint.sh"]
