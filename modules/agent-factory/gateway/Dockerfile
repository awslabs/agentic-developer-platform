# Agent Gateway — SQS Consumer (Pure Python)
# Uses Claude via Bedrock directly — no TypeScript, no subprocess.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    dumb-init curl && \
    rm -rf /var/lib/apt/lists/*

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
