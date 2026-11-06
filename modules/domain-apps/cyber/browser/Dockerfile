ARG PILLOW_SECURITY_IMAGE=879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:7b3df072a13b928bddbf70d71735fdc8572006442048868914627ebc8e635b68
FROM ${PILLOW_SECURITY_IMAGE} AS pillow-security
FROM public.ecr.aws/docker/library/python:3.13.16-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
RUN groupadd -g 1001 agent && useradd -u 1001 -g agent -m agent
WORKDIR /app
COPY modules/domain-apps/cyber/browser/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY --chown=agent:agent modules/tools/agentcore/agentcore_tools/ /app/agentcore_tools/
COPY --chown=agent:agent modules/domain-apps/cyber/agent/skills/url-analysis/ /app/skills/url-analysis/
# The source-built Pillow 13 wheel also fixes its bundled Brotli decoder.
RUN --network=none --mount=type=bind,from=pillow-security,source=/wheels,target=/pillow-wheels \
    python -m pip install --no-index --no-deps --force-reinstall --no-cache-dir /pillow-wheels/pillow-13.0.0+adp1-cp313-*.whl \
    && python -m pip check
COPY --from=pillow-security /maintenance/pillow/ /opt/adp-security/pillow/
COPY --from=pillow-security /maintenance/pillow-wheel-lock.json /opt/adp-security/pillow/wheel-lock.json
USER 1001:1001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app:/app/skills/url-analysis
EXPOSE 8765
ENTRYPOINT ["python3", "/app/skills/url-analysis/browser_broker.py"]
