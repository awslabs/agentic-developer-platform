# Agent Gateway — SQS Consumer (Pure Python)
# Uses Claude via Bedrock directly — no TypeScript, no subprocess.

FROM public.ecr.aws/docker/library/python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    dumb-init curl git bash unzip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install latest AWS CLI v2
RUN curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip && \
    unzip -q /tmp/awscli.zip -d /tmp && \
    /tmp/aws/install && \
    rm -rf /tmp/awscli.zip /tmp/aws

RUN groupadd -g 10001 agentgw && useradd -u 10001 -g agentgw -s /bin/sh agentgw

WORKDIR /app

COPY app/requirements.txt /app/app/requirements.txt
RUN pip install --no-cache-dir -r /app/app/requirements.txt

COPY app/ /app/app/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && chown -R agentgw:agentgw /app

USER agentgw:agentgw

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["dumb-init", "--"]
CMD ["/app/entrypoint.sh"]
