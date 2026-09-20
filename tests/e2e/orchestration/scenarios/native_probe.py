"""Transport for one inventoried, disposable native runner process."""

from pathlib import Path
import json
import subprocess
import time
from datetime import UTC, datetime

from .definitions import DEFINITION_HASH
from .http import Unsupported
from .runtime import read_runtime

OPERATIONS = {
    "tick-restart": "crash-after-intent",
    "timeout-after-success": "timeout-after-effect",
    "duplicate-events": "duplicate-events",
    "out-of-order": "out-of-order",
}


class NativeProbe:
    kind = "qualification-isolated-tick"

    def __init__(self, session):
        self.session = session
        self.finished = set()

    def request(self, execution_id, mode):
        session = self.session
        if not session.manifest.native_faults:
            raise Unsupported(
                "isolated native probes are not enabled in the accepted manifest"
            )
        if time.monotonic() >= session.started + session.config.max_duration_seconds:
            raise Unsupported("qualification duration exhausted")
        # Recheck the registered role and observed revision before every probe.
        # Native actions themselves recheck current policy and claim authority.
        target = session.manifest.runtime["engine"]
        runtime = read_runtime(session.client, target)
        allowed = {session.config.versions["engine"]}
        allowed.update(
            r["actual_revision"] for r in session.runtime_observations.values()
        )
        if runtime["actual_revision"] not in allowed:
            raise Unsupported("native probe target changed to an unverified revision")
        request = dict(
            mode=mode,
            org_id=session.config.org_ref,
            flow_id=session.flow_id,
            execution_id=execution_id,
            qualification_id=session.inventory.qualification_id,
            definition_hash=DEFINITION_HASH,
            plan_hash=session.accepted["plan_hash"],
            plan_version=session.accepted["plan_version"],
        )
        source = Path(__file__).with_name("native_process.py").read_text()
        command = [
            "kubectl",
            "--context",
            runtime["cluster"],
            "--request-timeout=15s",
            "-n",
            target.namespace,
            "exec",
            "deployment/" + target.deployment,
            "-c",
            target.container,
            "--",
            "python",
            "-c",
            source,
            json.dumps(request),
        ]
        try:
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=70
            )
        except (OSError, subprocess.SubprocessError):
            raise Unsupported(
                "isolated process outcome unknown; do not repeat injection"
            ) from None
        rows = [
            line.removeprefix("ADP_Q2_RESULT:")
            for line in process.stdout.splitlines()
            if line.startswith("ADP_Q2_RESULT:")
        ]
        if len(rows) != 1 or process.returncode not in {0, 3, 75}:
            raise Unsupported("isolated process produced no bounded native receipt")
        result = json.loads(rows[0])
        if result.get("status") == "NOT_RUN":
            raise Unsupported("native operation unavailable: " + result["reason"])
        self.finished.add(execution_id)
        return result

    def create(self, *, intended_identity, ownership_tags, idempotency_token):
        qualification_id, execution_id = intended_identity.split("/", 1)
        if (
            qualification_id != self.session.inventory.qualification_id
            or ownership_tags != self.session.config.ownership_tags(qualification_id)
        ):
            raise ValueError("native probe is not owned by this qualification")
        result = self.request(execution_id, "read")
        if result["after"]["execution_id"] != execution_id:
            raise ValueError("native execution scope mismatch")
        return execution_id

    def find(self, *, intended_identity, idempotency_token):
        execution_id = intended_identity.split("/", 1)[1]
        self.request(execution_id, "read")
        return execution_id

    def read_tags(self, resource_id):
        self.request(resource_id, "read")
        return self.session.config.ownership_tags(
            self.session.inventory.qualification_id
        )

    def delete(self, resource_id):
        if resource_id not in self.finished:
            raise Unsupported(
                "isolated process exit is not verified in this session; retain audit inventory"
            )
        # No service resource was created or deleted: the subprocess has exited.
        # Q1 retains its fixture/audit record. Never stop a pod or another PID.

    def inject(self, name, resource_id):
        if name not in OPERATIONS:
            raise Unsupported("no native adapter for this fault")
        observed = self.request(resource_id, OPERATIONS[name])
        if name == "tick-restart":
            injection = observed["injection"]
            if injection.get("checkpoint") != "after_intent":
                raise Unsupported("no actual durable-intent crash was injected")
            observed["before"] = observed["after"]
            # Recovery waits for the real scheduler due time. It never changes
            # next_check_at, resets authority, or manually triggers a coordinator.
            while (
                time.monotonic() - self.session.started + 10
                < self.session.config.max_duration_seconds
            ):
                after = self.request(resource_id, "read")["after"]
                due = after.get("next_check_at")
                if due and datetime.fromisoformat(due) <= datetime.now(UTC):
                    resumed = self.request(resource_id, "once")["after"]
                    injection["after_process_id"] = resumed["process_id"]
                    observed["after"] = resumed
                    return observed
                if after["terminal"]:
                    raise Unsupported(
                        "shared scheduler recovered before the isolated restart; do not claim isolated continuation"
                    )
                time.sleep(min(self.session.manifest.poll_seconds, 10))
            raise Unsupported(
                "continuation after isolated exit was not observed within the bound"
            )
        return observed
