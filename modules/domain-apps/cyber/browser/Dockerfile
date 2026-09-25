FROM python:3.13-slim
RUN groupadd -g 1001 agent && useradd -u 1001 -g agent -m agent
WORKDIR /app
COPY browser/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY --chown=agent:agent agent/skills/url-analysis/ /app/skills/url-analysis/
USER 1001:1001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8765
ENTRYPOINT ["python3", "/app/skills/url-analysis/browser_broker.py"]
