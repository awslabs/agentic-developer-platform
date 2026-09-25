FROM public.ecr.aws/lambda/python:3.12@sha256:9d05e09dee76e344595bc017f9efd7359d353ee6f1018b17b722b048f6033ab8
RUN pip install --no-cache-dir boto3==1.36.0 requests==2.34.2 fastapi==0.136.3 pydantic==2.12.5 rfc8785==0.1.4
COPY modules/tools/adp_tools ${LAMBDA_TASK_ROOT}/adp_tools
COPY modules/domain-apps/cyber/tools/cyber_tools ${LAMBDA_TASK_ROOT}/cyber_tools
# Lambda needs only read access to code/dependencies and writable /tmp.
USER 65532:65532
CMD ["cyber_tools.handler.lambda_handler"]
