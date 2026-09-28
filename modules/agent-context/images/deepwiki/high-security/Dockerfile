FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:43eec354851625f1d65684479f9471dfed491596aab5ca8706b72a7aac23c787
USER root
RUN /usr/local/bin/python3.11 -m pip install --no-cache-dir --upgrade setuptools==84.0.0 && /usr/local/bin/python3.11 -m pip check
USER 10001:10001
