"""Read original human UI facts from the current protected release, never open a UI."""

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from qualification_human_observations import digest
from qualification_observation_wait import collect_by_scope
from release_context import release_identity


class ManualEvidencePending(ValueError):
    code = "devops_human_evidence_pending"
    next_action = ("Complete the selected DevOps UI submission and two ADP-to-DevOps SSO observations in the original "
                   "release window. Read retained receipts; do not repeat native work or enable browser automation.")


def validate_manual(manifest):
    selected = manifest.get("manual_ui")
    if selected is None:
        return None
    users = manifest.get("recovery", {}).get("users", [])
    if (not isinstance(selected, dict) or set(selected) != {
            "mode", "preparer_id", "observer_ids", "ui_observer_id", "ui_receipt", "timeout_seconds", "poll_seconds"}
            or selected["mode"] != "authenticated-human"
            or not isinstance(selected["preparer_id"], str) or not selected["preparer_id"]
            or not isinstance(selected["observer_ids"], list) or len(selected["observer_ids"]) != 2
            or any(not isinstance(user, str) or not user for user in selected["observer_ids"])
            or len(set(selected["observer_ids"])) != 2
            or set(selected["observer_ids"]) != {user["id"] for user in users}
            or selected["ui_observer_id"] not in selected["observer_ids"]
            or not isinstance(selected["ui_receipt"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", selected["ui_receipt"])
            or selected["ui_receipt"] in manifest.get("journey_driver", {}).get("receipts", {}).values()
            or type(selected["timeout_seconds"]) is not int or not 1 <= selected["timeout_seconds"] <= 900
            or type(selected["poll_seconds"]) is not int or not 1 <= selected["poll_seconds"] <= 30):
        raise ValueError("DevOps human mode requires two distinct original observers, a stable UI receipt and bounded wait")
    return selected


class Journal:
    def __init__(self, manifest, directory, environment):
        self.binding = {"context": release_identity(environment)}
        self.path = Path(directory) / f"devops-{manifest['authorization']['run_id']}-human.jsonl"

    def append(self, kind, value):
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(json.dumps({"release": self.binding["context"], "kind": kind, "value": value}, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


class ManualDevops:
    def __init__(self, manifest, directory, *, deadline, environment=None):
        self.manifest = manifest
        self.selected = validate_manual(manifest)
        if self.selected is None:
            raise ValueError("The original fixture has no human DevOps evidence selection")
        self.environment = dict(os.environ if environment is None else environment)
        identity = release_identity(self.environment)
        if (identity["service"] != "devops" or not identity["correlation"] or not identity["contract_digest"]
                or identity["source_revision"] != manifest["release"]["source_sha"]):
            raise ValueError("Human DevOps evidence requires the original engine-owned release")
        self.journal = Journal(manifest, directory, self.environment)
        self.deadline = deadline

    def collect(self, configuration, *, observer, subject, check_id, operation=None, request_sha256=None):
        driver = self.manifest["journey_driver"]
        scope = driver["scope"]
        if (configuration.get("revision") != driver["inputs"]["ui"]["configuration_revision"]
                or any(configuration.get("scope", {}).get(key) != scope[key] for key in ("product_id", "project_id"))
                or not configuration.get("configuration_id") or observer not in self.selected["observer_ids"]):
            raise ValueError("Human DevOps evidence selection differs from the current fixture configuration")
        selection = {**scope, "service_id": "devops", "configuration_id": configuration["configuration_id"],
                     "configuration_revision": configuration["revision"], "subject": subject, "check_id": check_id,
                     "result_id": None, "result_revision": None, "result_run_id": None}
        request = {"selection": selection, "preparer_id": self.selected["preparer_id"], "observer_id": observer,
                   "evidence": "effect" if operation else "human"}
        if operation:
            request.update(operation=operation, request_sha256=request_sha256)
        remaining = min(self.selected["timeout_seconds"], int(self.deadline - time.monotonic()))
        if remaining < 1:
            raise ManualEvidencePending(ManualEvidencePending.next_action)
        print(f"DevOps {subject} awaits the selected person in ADP for check {check_id}; browser automation is disabled.", flush=True)
        try:
            return collect_by_scope(self.journal, [request], environment=self.environment,
                                    timeout_seconds=remaining, poll_seconds=self.selected["poll_seconds"])[0]
        except TimeoutError:
            raise ManualEvidencePending(ManualEvidencePending.next_action) from None

    def submission(self, configuration):
        observer = self.selected["ui_observer_id"]
        arguments = {"observer": observer, "subject": "run_submission", "check_id": "ui-cli-mcp-parity"}
        expected = digest({"input": self.manifest["journey_driver"]["inputs"]["ui"], "idempotency_key": self.selected["ui_receipt"]})
        effect = self.collect(configuration, **arguments, operation="submit", request_sha256=expected).facts["effect"]
        human = self.collect(configuration, **arguments).facts["observation"]
        after = effect["events"][-1]
        if (effect["outcome"] != "returned" or after.get("http_status") != 202
                or effect["challenge_id"] != human["challenge_id"]
                or effect["snapshot_sha256"] != human["snapshot_sha256"]
                or human["actor_id"] != observer or human["observed"]["observation"] != "submitted"
                or human["observed"].get("submitted_run_id") != after.get("returned_run_id")
                or not after.get("returned_run_id")):
            raise ValueError("UI submission needs the same human assertion and successful canonical request effect")
        self.arrival(configuration, observer)
        return {"run_id": after["returned_run_id"], "receipt": self.selected["ui_receipt"], "native_handoff": True}

    def arrival(self, configuration, observer):
        human = self.collect(configuration, observer=observer, subject="native_ui", check_id="two-user-sso").facts["observation"]
        observed = human["observed"]
        if (human["actor_id"] != observer or observed["observation"] != "displayed"
                or observed["displayed_native_hostname"] != self.manifest["journey_driver"]["provider_hostname"]
                or not observed["launch_receipt_id"]):
            raise ValueError("Human SSO arrival needs the exact native host and independently verified launch chain")
        return human
