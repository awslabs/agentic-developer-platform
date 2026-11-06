ARG PYTHON_BASE
FROM ${PYTHON_BASE}

WORKDIR /app
COPY --chmod=0444 requirements.lock .
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock
COPY --chmod=0444 load_credentials.py .
COPY --chmod=0555 fleeting-plugin-aws /opt/fleeting/fleeting-plugin-aws
USER 65532:65532
ENTRYPOINT ["python", "/app/load_credentials.py"]
