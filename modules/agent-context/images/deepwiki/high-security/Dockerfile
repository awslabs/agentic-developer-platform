FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:17c3cabaf3804f8fbd6c81053fc4f48d8dcf1b2acc05268c5c87d89971b005a5
USER root
RUN /usr/local/bin/python3.11 -m pip install --no-cache-dir --upgrade setuptools==84.0.0 && /usr/local/bin/python3.11 -m pip check
USER 10001:10001
