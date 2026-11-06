"""Protected fault and readback transport; no local service or worker provisioned."""

import hashlib
import json
import os
import re
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from assessment_live import LiveAssessmentDriver
from clients import api_reader, product_sources
from clients_setup import LiveSetupDriver
from evidence import AttemptJournal


ROOT = Path(__file__).resolve().parents[4]


def validate_recovery_fixture(case, foreign_case):
    selection = case.get("recovery")
    if not isinstance(selection, dict) or selection.get("authorized_faults") is not True:
        raise ValueError("private owner authorization for worker faults required")
    path = Path(selection["journal_file"])
    snapshot = Path(str(path) + ".fault.json")
    if (not path.is_absolute() or path.is_relative_to(ROOT) or path.exists() or snapshot.exists() or snapshot.is_symlink()
            or not path.parent.is_dir() or path.parent.stat().st_mode & 0o077):
        raise ValueError("recovery attempts require a fresh private journal outside the checkout")
    url = urlsplit(selection["fault_url"])
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment):
        raise ValueError("owner-supplied fault control must be an HTTPS origin")
    for field in ("fault_token_env", "revoked_token_env"):
        if not os.environ.get(selection[field]):
            raise ValueError("missing independently authorized fault or denial credential")
    if (foreign_case["scope"]["tenant_id"] == case["scope"]["tenant_id"]
            or os.environ[selection["fault_token_env"]] in {
                os.environ.get(case.get("api_token_env", "")), os.environ[selection["revoked_token_env"]]}
            or selection.get("exclusive_agent_space") is not True
            or selection.get("exclusive_worker_pool") is not True
            or not isinstance(selection.get("owned_binding_id"), str)
            or not selection["owned_binding_id"] or selection["owned_binding_id"] == selection["binding_id"]
            or len(selection.get("owned", [])) != 1 or len(selection.get("linked", [])) != 1):
        raise ValueError("fault injection and separate owned cleanup require Product-scoped fixtures")
    if (selection["max_attempts"] < 1 or selection["max_attempts"] > case["max_attempts"]
            or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", selection["receipt"]) is None
            or not selection["foreign_product_id"]
            or selection["foreign_product_id"] == case["scope"]["product_id"]):
        raise ValueError("missing attempt bound, receipt or denied Product counterexample")


def recovery_progress(before, after, entries):
    previous = before["work"]
    current = after["work"]
    work_progressed = (previous.get("work_id") == current.get("work_id")
                       and (previous.get("completed_at") is not None
                            or previous.get("last_enqueued_at") and current.get("last_enqueued_at")
                            and previous["last_enqueued_at"] != current["last_enqueued_at"]
                            and current.get("attempts", 0) >= previous.get("attempts", 0)))
    return {"work_progressed": work_progressed,
            "operator_command": any(entry["operation"] == "run_reconcile" for entry in entries)}


class LiveRecoveryDriver:
    def __init__(self, case, foreign_case):
        validate_recovery_fixture(case, foreign_case)
        self.case, self.foreign_case = case, foreign_case
        self.deadline = time.monotonic() + case["deadline_seconds"]
        self.api = httpx.Client(base_url=case["api_url"], timeout=20, follow_redirects=False,
                                headers={"Authorization": "Bearer " + os.environ[case["api_token_env"]]})
        self.read_api = api_reader(self.api)
        self.journal = AttemptJournal(case["recovery"]["journal_file"])
        self.run_id = None
        self.setup = object.__new__(LiveSetupDriver)
        self.setup.case, self.setup.deadline = case, self.deadline
        self.setup.mcp = httpx.Client(base_url=case["api_url"], timeout=20, follow_redirects=False,
                                     headers={"Authorization": "Bearer " + os.environ[case["mcp_token_env"]]})
        self.setup.journal = self.journal
        self.native = object.__new__(LiveAssessmentDriver)
        self.native.case, self.native.deadline = case, self.deadline
        self.native.api, self.native.read_api, self.native.setup = self.api, self.read_api, self.setup

    def close(self):
        self.api.close()
        self.setup.mcp.close()

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("recovery exceeded the authorized deadline; retain private evidence")
        return left

    def _request(self, method, url, token_env, *, body=None):
        self.remaining()
        with httpx.Client(timeout=min(20, self.remaining()), follow_redirects=False) as client:
            response = client.request(method, url, json=body,
                                      headers={"Authorization": "Bearer " + os.environ[token_env]})
        response.raise_for_status()
        return response.json()

    def _journal_effect(self, operation, status, work_id, effect_id=None):
        self.journal.append({"attempt_id": operation, "operation": operation, "status": status,
                             "run_id": self.run_id, "work_id": work_id, "effect_id": effect_id,
                             "observed_at": datetime.now(UTC).isoformat()})

    def check_selection(self, selection):
        scope = self.case["scope"]
        path = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                f"{quote(scope['project_id'], safe='')}/services/security")
        config = self.read_api(path + "/configuration")
        binding = self.read_api(path + "/bindings/" + quote(selection["binding_id"], safe=""))
        if (config["scope"] != scope or binding["scope"] != scope
                or config["configuration_id"] != self.case["selected"]["configuration_id"]
                or config["revision"] != selection["input"]["configuration_revision"]
                or binding["binding_id"] != selection["binding_id"]
                or binding["configuration_id"] != config["configuration_id"]
                or binding["state"] != "ready"
                or binding["provider_identity"] != self.case["selected"]["provider_identity"]):
            raise AssertionError("fault target differs from independently read selected configuration or binding")

    def arm_faults(self, selection):
        self._journal_effect("fault_arm", "prepared", selection["receipt"])
        response = self._request("POST", selection["fault_url"], selection["fault_token_env"], body={
            "action": "arm", "receipt": selection["receipt"],
            "faults": ["uncertain_submit_ack", "publication_ack"]})
        if (response.get("receipt") != selection["receipt"] or response.get("armed") is not True
                or response.get("scope") != self.case["scope"]):
            raise AssertionError("authorized fault boundaries were not armed")
        self._journal_effect("fault_arm", "acknowledged", selection["receipt"])

    def submit(self, selection):
        self.check_selection(selection)
        self._journal_effect("run_submit", "prepared", selection["receipt"])
        try:
            response = self.setup.mcp_call("l5___submit_run", {**self.setup._scope(),
                                           "input": selection["input"],
                                           "idempotency_key": selection["receipt"]}, status=202)
        except (httpx.TimeoutException, httpx.TransportError):
            return
        self.run_id = response["run_id"]
        self._journal_effect("run_submit", "acknowledged", selection["receipt"], self.run_id)

    def _product_path(self):
        scope = self.case["scope"]
        return (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                f"{quote(scope['project_id'], safe='')}/services/security")

    def _worker(self, selection):
        selected = self.read_api(self._product_path() + "/runs/receipts/" +
                                 quote(selection["receipt"], safe=""))
        run_id = selected.get("run_id") if isinstance(selected, dict) else None
        if not isinstance(run_id, str) or not run_id or self.run_id is not None and run_id != self.run_id:
            raise AssertionError("submission receipt does not identify the selected run")
        observed = self.read_api(self._product_path() + "/runs/" + quote(run_id, safe=""))
        if (not isinstance(observed, dict) or observed.get("run_id") != run_id
                or observed.get("scope") != self.case["scope"]
                or observed.get("input") != selection["input"]
                or observed.get("input", {}).get("sources") != selection["sources"]
                or observed.get("input", {}).get("binding_id") != selection["binding_id"]):
            raise AssertionError("submission receipt differs from Product-authorized run")
        sources = product_sources(observed, self.api)
        if not sources or [item["source_ref"] for item in sources] != selection["sources"]:
            raise AssertionError("recovered work differs from retained Product source revisions")
        durable = self.read_api(self._product_path() + "/runs/" + quote(run_id, safe="") + "/recovery-state")
        if not isinstance(durable, dict) or durable.get("run") != observed:
            raise AssertionError("Product run changed while reading recovery state")
        attempts = durable.get("attempts")
        work = durable.get("work")
        if (not isinstance(attempts, list) or not isinstance(work, list)
                or any(not isinstance(item, dict) or not item.get("attempt_id")
                       or not item.get("first_work_id") or not item.get("started_at")
                       for item in attempts)
                or len({item["attempt_id"] for item in attempts}) != len(attempts)):
            raise AssertionError("Product recovery attempts or work are incomplete")
        submitted = [item for item in attempts if item["operation"] == "submit"]
        if len(submitted) != 1 and (submitted or attempts):
            raise AssertionError("Product recovery has no unique native submit attempt")
        owned_work = ([item for item in work if item.get("work_id") == submitted[0]["first_work_id"]]
                      if submitted else [])
        if submitted and len(owned_work) != 1:
            raise AssertionError("Product recovery has no unique native submit work")
        run = {**observed, "attempts": [{**item, "work_id": item["first_work_id"],
                                          "observed_at": item["started_at"]} for item in attempts],
               "work": owned_work[0] if owned_work else {}}
        self.run_id = run_id
        return {"run": run}

    def _publication(self, selection):
        durable = self.read_api(self._product_path() + "/runs/" +
                                quote(self.run_id, safe="") + "/recovery-state")
        publications = durable.get("publications") if isinstance(durable, dict) else None
        if (not isinstance(durable, dict) or durable.get("run", {}).get("run_id") != self.run_id
                or not isinstance(publications, list) or len(publications) > 1):
            raise AssertionError("Product recovery has no unique publication for this run")
        if not publications:
            return None
        publication = publications[0]
        if (not isinstance(publication, dict) or not isinstance(publication.get("publication_id"), str)
                or not isinstance(publication.get("receipt"), (dict, type(None)))):
            raise AssertionError("Product recovery has an invalid publication")
        observed = self.read_api(self._product_path() + "/publications/" +
                                 quote(publication["publication_id"], safe=""))
        if (not isinstance(observed, dict) or any(observed.get(field) != publication.get(field)
                for field in ("publication_id", "result_id", "state"))
                or (publication.get("state") == "published" and (
                    observed.get("receipt_id") != (publication.get("receipt") or {}).get("receipt_id")
                    or observed.get("consumer_revision") != (publication.get("receipt") or {}).get("consumer_revision")))):
            raise AssertionError("publication state differs between Product readbacks")
        return publication

    def read_faulted(self, selection):
        while True:
            try:
                worker = self._worker(selection)
                publication = self._publication(selection)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 404:
                    raise
            else:
                if publication and publication["state"] == "retry_wait" and worker["run"]["attempts"]:
                    snapshot = {"receipt": selection["receipt"], "run": worker["run"],
                                "publication": publication, "observed_at": datetime.now(UTC).isoformat()}
                    content = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    if len(content) > 262144:
                        raise ValueError("fault-time readback exceeds private snapshot bound")
                    path = Path(str(self.journal.path) + ".fault.json")
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    try:
                        with os.fdopen(descriptor, "wb", closefd=False) as destination:
                            destination.write(content)
                            destination.flush()
                            os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                    return worker["run"], publication
            time.sleep(min(60, self.remaining()))

    def restart_workers(self, selection):
        self._journal_effect("worker_restart", "prepared", selection["receipt"])
        response = self._request("POST", selection["fault_url"], selection["fault_token_env"], body={
            "action": "restart", "receipt": selection["receipt"], "run_id": self.run_id})
        if (response.get("run_id") != self.run_id or response.get("restarted") is not True
                or response.get("scope") != self.case["scope"]):
            raise AssertionError("authorized worker restart was not acknowledged")
        self._journal_effect("worker_restart", "acknowledged", selection["receipt"], self.run_id)

    def read_recovered(self, selection):
        while True:
            worker = self._worker(selection)
            publication = self._publication(selection)
            if publication and worker["run"]["state"] == "succeeded" and publication["state"] == "published":
                path = Path(str(self.journal.path) + ".fault.json")
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if (not stat.S_ISREG(os.fstat(descriptor).st_mode)
                            or os.fstat(descriptor).st_mode & 0o077
                            or os.fstat(descriptor).st_size > 262144):
                        raise ValueError("fault-time snapshot lost its private evidence bound")
                    with os.fdopen(descriptor, "r", closefd=False) as source:
                        snapshot = json.load(source)
                finally:
                    os.close(descriptor)
                return worker["run"], publication, recovery_progress(
                    snapshot["run"], worker["run"], self.journal.read())
            time.sleep(min(60, self.remaining()))

    def retain_attempts(self, attempts):
        existing = {entry["attempt_id"]: entry for entry in (
            self.journal.read() if self.journal.path.exists() else [])}
        for attempt in attempts:
            attempt_id = attempt["attempt_id"]
            if attempt_id in existing:
                prior_job = existing[attempt_id]["native_job_id"]
                current_job = (attempt.get("native_job_ref") or {}).get("record_id")
                if prior_job is not None and prior_job != current_job:
                    raise AssertionError("durable worker attempt changed during recovery")
                continue
            self.journal.append({"attempt_id": attempt_id, "run_id": self.run_id,
                                 "operation": attempt["operation"], "status": attempt["status"],
                                 "work_id": attempt["work_id"], "native_job_id": (
                                     attempt.get("native_job_ref") or {}).get("record_id"),
                                 "observed_at": attempt["observed_at"]})

    def recorded_attempts(self):
        return [entry for entry in self.journal.read() if entry["operation"] not in {
            "fault_arm", "run_submit", "worker_restart", "cleanup"}]

    def scope_probes(self, run_id, selection, foreign_case):
        return read_scope_probes(self.case, run_id, selection, foreign_case, self.api,
                                 os.environ.__getitem__, self.remaining)

    def native_jobs(self, after):
        return [{"native_job_ref": ref} for ref in self.native._native("inventory", after)]

    def _cleanup_packet(self, selection):
        packet = self.read_api(self._product_path() + "/runs/" + quote(self.run_id, safe="") +
                               "/native-readback/spaces/" + quote(selection["owned_binding_id"], safe=""))
        expected = {selection["owned"][0]: "scenario_owned", selection["linked"][0]: "customer_linked"}
        if (not isinstance(packet, dict) or packet.get("run_id") != self.run_id
                or packet.get("ownership") != expected or not isinstance(packet.get("resources"), dict)
                or set(packet["resources"]) != set(expected)
                or packet["resources"][selection["linked"][0]] != "retained"):
            raise AssertionError("Product native ownership differs from the selected cleanup")
        return packet

    def _owned_binding(self, selection):
        binding = self.read_api(self._product_path() + "/bindings/" +
                                quote(selection["owned_binding_id"], safe=""))
        if (not isinstance(binding, dict) or binding.get("binding_id") != selection["owned_binding_id"]
                or binding.get("scope") != self.case["scope"]
                or binding.get("configuration_id") != self.case["selected"]["configuration_id"]
                or binding.get("ownership") != "adp_created"
                or not isinstance(binding.get("native_resource_ref"), dict)
                or binding["native_resource_ref"].get("record_id") != selection["owned"][0]
                or type(binding.get("revision")) is not int or binding["revision"] < 1):
            raise AssertionError("Product binding does not authorize selected owned cleanup")
        return binding

    def validate_cleanup_ownership(self, selection):
        binding = self._owned_binding(selection)
        packet = self._cleanup_packet(selection)
        if binding["state"] != "ready" or packet["resources"][selection["owned"][0]] != "present":
            raise AssertionError("independent native ownership does not authorize fixture cleanup")

    def cleanup_owned(self, selection):
        binding = self._owned_binding(selection)
        if binding["state"] != "ready":
            raise AssertionError("owned binding changed before cleanup")
        self._journal_effect("cleanup", "prepared", self.run_id)
        key = "security-cleanup-" + hashlib.sha256(
            (self.run_id + ":" + selection["owned_binding_id"]).encode()).hexdigest()
        response = self.api.post(self._product_path() + "/binding-operations", json={
            "action": "cleanup", "configuration_revision": selection["input"]["configuration_revision"],
            "binding_id": selection["owned_binding_id"], "owned_cleanup_confirmed": True}, headers={
                "If-Match": f'"{binding["revision"]}"', "Idempotency-Key": key})
        response.raise_for_status()
        operation = response.json()
        if response.status_code != 202 or operation.get("action") != "cleanup" or not operation.get("operation_id"):
            raise AssertionError("Product cleanup operation was not accepted")
        while True:
            current = self.read_api(self._product_path() + "/binding-operations/" +
                                    quote(operation["operation_id"], safe=""))
            if current.get("operation_id") != operation["operation_id"] or current.get("action") != "cleanup":
                raise AssertionError("owned cleanup operation changed")
            if current["state"] == "succeeded":
                break
            if current["state"] != "pending":
                raise AssertionError("Product cleanup did not succeed")
            time.sleep(min(10, self.remaining()))
        if (self._owned_binding(selection)["state"] != "cleaned"
                or self._cleanup_packet(selection)["resources"][selection["owned"][0]] != "absent"):
            raise AssertionError("owned cleanup has no native absence readback")
        self._journal_effect("cleanup", "acknowledged", self.run_id)

    def cleanup_readback(self, selection):
        if self._owned_binding(selection)["state"] != "cleaned":
            raise AssertionError("Product cleanup state changed")
        return self._cleanup_packet(selection)["resources"]


def read_scope_probes(case, run_id, selection, foreign_case, client, get_token, remaining):
    scope = case["scope"]
    target = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
              f"{quote(scope['project_id'], safe='')}/services/security/runs/{quote(run_id, safe='')}")
    foreign_product = target.replace("/products/" + quote(scope["product_id"], safe=""),
                                     "/products/" + quote(selection["foreign_product_id"], safe=""), 1)
    probes = []
    for name, path, token in (("foreign_product", foreign_product, get_token(case["api_token_env"])),
                              ("foreign_tenant", target, get_token(foreign_case["api_token_env"])),
                              ("revoked_access", target, get_token(selection["revoked_token_env"]))):
        remaining()
        response = client.get(path, headers={"Authorization": "Bearer " + token})
        if len(response.content) > 8192:
            raise ValueError("scope denial response exceeds its evidence bound")
        body = response.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        allowed = (isinstance(body, dict) and set(body) == {"detail"} and (
            detail in ("Not Found", "Forbidden") if isinstance(detail, str) else
            isinstance(detail, dict) and set(detail) == {"code"} and
            detail["code"] in {"not_found", "forbidden", "access_denied"}))
        serialized = json.dumps(body, sort_keys=True)
        disclosed = [value for value in (run_id, "native_job_ref", "result_id",
                        "attempts", case["scope"]["tenant_id"]) if value in serialized]
        if not allowed:
            disclosed.append("unexpected_denial_payload")
        probes.append({"case": name, "status": response.status_code,
                       "disclosed_resources": disclosed})
    return probes
