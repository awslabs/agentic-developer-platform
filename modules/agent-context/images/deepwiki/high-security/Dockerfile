FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-dev-agent-context-deepwiki@sha256:ca17d314fae4f6ec71bd94482c6217d1acf9ce3ea264912bc0072e965f81d144
USER root
RUN /usr/local/bin/python3.11 -m pip install --no-cache-dir --upgrade setuptools==84.0.0 && /usr/local/bin/python3.11 -m pip check
USER 10001:10001
