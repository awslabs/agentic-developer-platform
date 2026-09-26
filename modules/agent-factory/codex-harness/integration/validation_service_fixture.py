"""Local service delivery seam: real admission, artifact reads and Kubernetes.

Only HTTP/IAM delivery and asynchronous Lambda scheduling are substituted.
The separate service authority resolves the real Moto gateway Task ledger.
"""

from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sys
import threading

import boto3

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/tools"))
sys.path.insert(0, str(ROOT / "modules/tools/validation"))
from adp_tools.authority import TaskHostAuthority  # noqa: E402
from adp_tools.storage import OperationRepository  # noqa: E402
from lib.codex_kubernetes_validation import from_host_configuration  # noqa: E402
from validation_tools.operations import ValidationBody, execute_job, public_job  # noqa: E402
from validation_tools.store import ValidationJobs  # noqa: E402


class ValidationServiceFixture:
    def __init__(self, gateway):
        self.gateway = gateway
        self.client = boto3.client("dynamodb", region_name="us-east-1")
        self.table = "qualification-validation-jobs"
        self.client.create_table(TableName=self.table, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ["event_id", "arrived_at"]])
        self.threads = []

    def call(self, raw):
        body = ValidationBody.model_validate(raw)
        authority = TaskHostAuthority(lambda action, request: self.gateway._post(action, request, run_bound=True))
        attempt = body.attempt.model_dump()
        def authorize():
            return authority.authorize(attempt=attempt,
                tool="validation.cancel_jobs" if body.operation == "cancel_jobs" else "validation.run",
                cleanup=body.operation == "cancel_jobs")
        identity = authorize().identity
        jobs = ValidationJobs(OperationRepository(self.client, self.table, authorize))
        if body.operation == "inspect":
            executor = from_host_configuration(identity.task_id)
            executor._boundary()
            return {"schema_version": "1.0", "idle": jobs.idle(identity)}
        if body.operation == "cancel_jobs":
            pending = jobs.close(identity)
            return {"schema_version": "1.0", "phase": "pending" if pending else "cancelled", "pending": pending}
        if body.operation == "run":
            row, _ = jobs.admit(identity, body.operation_id, body.payload.model_dump())
        else:
            row = jobs.read(identity, body.operation_id)
        if row["phase"] == "pending" and jobs.delivery(identity, body.operation_id):
            @contextmanager
            def executor_factory(task_id):
                yield from_host_configuration(task_id)
            thread = threading.Thread(target=execute_job, kwargs=dict(jobs=jobs, authority=authority, attempt=attempt,
                identity=identity, operation_id=body.operation_id, executor_factory=executor_factory, remaining_ms=lambda: 180000))
            thread.start()
            self.threads.append(thread)
        return json.loads(json.dumps(public_job(row), default=lambda value: int(value) if isinstance(value, Decimal) and value == value.to_integral_value() else float(value)))
