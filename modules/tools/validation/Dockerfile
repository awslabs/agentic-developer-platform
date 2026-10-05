FROM public.ecr.aws/lambda/python:3.12@sha256:9d05e09dee76e344595bc017f9efd7359d353ee6f1018b17b722b048f6033ab8
RUN dnf install -y git && dnf clean all
RUN pip install --no-cache-dir boto3==1.36.0 requests==2.34.2 fastapi==0.136.3 pydantic==2.12.5 rfc8785==0.1.4
COPY modules/tools/adp_tools ${LAMBDA_TASK_ROOT}/adp_tools
COPY modules/tools/validation/validation_tools ${LAMBDA_TASK_ROOT}/validation_tools
COPY modules/agent-factory/agent-worker-image/lib/__init__.py modules/agent-factory/agent-worker-image/lib/codex_source.py modules/agent-factory/agent-worker-image/lib/codex_source_limits.py modules/agent-factory/agent-worker-image/lib/codex_workspace.py modules/agent-factory/agent-worker-image/lib/codex_validation.py modules/agent-factory/agent-worker-image/lib/codex_validation_source.py modules/agent-factory/agent-worker-image/lib/codex_validation_service_runner.py modules/agent-factory/agent-worker-image/lib/codex_kubernetes_validation.py modules/agent-factory/agent-worker-image/lib/task_errors.py ${LAMBDA_TASK_ROOT}/lib/
USER 65532:65532
CMD ["validation_tools.handler.lambda_handler"]
