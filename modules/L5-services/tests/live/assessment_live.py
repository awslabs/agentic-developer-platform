"""Authorized Product, source and provider observations for the assessment selector."""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from clients import api_reader, product_sources, read_assessment
from clients_setup import LiveSetupDriver
from evidence import AttemptJournal
from providers import verify_assessment


def validate_assessment_fixture(case):
    plans = case.get("assessments")
    event = case.get("trigger_event")
    if (not isinstance(plans, list) or len(plans) != 2 or not isinstance(event, dict)
            or case.get("max_attempts", 0) < 4 or not case.get("assessment_journal_file")
            or not Path(case["assessment_journal_file"]).is_absolute()
            or Path(case["assessment_journal_file"]).is_relative_to(Path(__file__).resolve().parents[4])
            or not Path(case["assessment_journal_file"]).parent.is_dir()
            or Path(case["assessment_journal_file"]).parent.stat().st_mode & 0o077
            or Path(case["assessment_journal_file"]).exists()
            or Path(case["assessment_journal_file"] + ".event-before.json").exists()
            or Path(case["assessment_journal_file"] + ".event-before.json").is_symlink()
            or not case.get("provider", {}).get("region")
            or not event.get("receipt_url") or not event.get("token_env")
            or event.get("exclusive_agent_space") is not True):
        raise ValueError("protected assessment target, provider, journal and four attempt slots required")
    for plan in [*plans, event]:
        if (not plan.get("review_file")
                or not Path(plan["review_file"]).is_absolute()
                or Path(plan["review_file"]).is_relative_to(Path(__file__).resolve().parents[4])
                or (Path(plan["review_file"]).exists() and (Path(plan["review_file"]).is_symlink()
                    or not Path(plan["review_file"]).is_file()
                    or Path(plan["review_file"]).stat().st_mode & 0o077))):
            raise ValueError("private independent source and review evidence required")
        if (not plan["input"].get("sources") or any(
                not isinstance(source.get("record_revision"), str)
                or source["record_revision"].lower() in {"latest", "head", "null"}
                for source in plan["input"]["sources"])):
            raise ValueError("Product-scoped sources must pin the exact revision")
    if quote(event["event_id"], safe="") not in urlsplit(event["receipt_url"]).path:
        raise ValueError("event receipt URL must identify the selected event")
    for url in (event["url"], event["receipt_url"]):
        parts = urlsplit(url)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment):
            raise ValueError("event endpoints require protected HTTPS without embedded credentials")
    if not os.environ.get(event["token_env"]):
        raise ValueError("authorized source event credential required")


class LiveAssessmentDriver:
    def __init__(self, case):
        validate_assessment_fixture(case)
        self.case = case
        self.deadline = time.monotonic() + case["deadline_seconds"]
        self.api = httpx.Client(base_url=case["api_url"], timeout=20, follow_redirects=False,
                                headers={"Authorization": "Bearer " + os.environ[case["api_token_env"]]})
        self.mcp = httpx.Client(base_url=case["api_url"], timeout=20, follow_redirects=False,
                                headers={"Authorization": "Bearer " + os.environ[case["mcp_token_env"]]})
        self.read_api = api_reader(self.api)
        self.journal = AttemptJournal(case["assessment_journal_file"])
        if self.journal.path.exists():
            raise AssertionError("previous assessment attempts require reconciliation before a new paid run")
        self.setup = object.__new__(LiveSetupDriver)
        self.setup.case, self.setup.deadline = case, self.deadline
        self.setup.mcp, self.setup.journal = self.mcp, self.journal
        self.runs_by_capability = {}

    def close(self):
        self.api.close()
        self.mcp.close()

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("assessment exceeded authorized deadline; retain evidence and inspect native state")
        return left

    def submit(self, plan):
        self.remaining()
        scope = self.case["scope"]
        path = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                f"{quote(scope['project_id'], safe='')}/services/security")
        config = self.read_api(path + "/configuration")
        binding = self.read_api(path + "/bindings/" + quote(plan["binding_id"], safe=""))
        if (config["scope"] != scope or binding["scope"] != scope
                or binding["configuration_id"] != config["configuration_id"]
                or binding["state"] != "ready" or plan["input"]["configuration_revision"] != config["revision"]
                or any(binding["provider_identity"].get(field) != self.case["provider"][field]
                       for field in ("account_id", "role_arn", "region"))):
            raise AssertionError("current Product configuration or provider binding differs from selection")
        self.setup.record(plan["receipt"], "assessment_submit", "prepared")
        submitted = self.setup.mcp_call("l5___submit_run", {**self.setup._scope(), "input": plan["input"],
                                                        "idempotency_key": plan["receipt"]}, status=202)
        self.setup.record(plan["receipt"], "assessment_submit", "acknowledged", submitted["run_id"], plan["receipt"])
        return submitted

    def await_result(self, run_id):
        scope = self.case["scope"]
        base = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                f"{quote(scope['project_id'], safe='')}/services/security")
        while True:
            self.remaining()
            run = self.read_api(base + "/runs/" + quote(run_id, safe=""))
            if run["state"] in {"failed", "blocked", "cancelled", "expired", "outcome_unknown"}:
                raise AssertionError("run did not finish successfully; inspect private attempts and provider state")
            if run["state"] == "succeeded":
                self.runs_by_capability[run["input"]["capability"]] = run
                break
            time.sleep(min(600, self.remaining()))
        cursor, visited = None, set()
        while True:
            self.remaining()
            path = base + "/results?limit=100" + ("&cursor=" + quote(cursor, safe="") if cursor else "")
            page = self.read_api(path)
            matches = [row for row in page["items"] if row["run_id"] == run_id]
            if len(matches) > 1:
                raise AssertionError("more than one result for selected assessment")
            if matches:
                return read_assessment(scope, run_id, matches[0]["result_id"], self.read_api)
            cursor = page.get("next_cursor")
            if cursor and (cursor in visited or len(visited) >= 100):
                raise AssertionError("result pagination is incomplete")
            if cursor:
                visited.add(cursor)
            else:
                time.sleep(min(600, self.remaining()))

    def native_job(self, run):
        return self._native("job", run)

    def _space(self, run):
        return (run["admission"]["native_space_ref"]["record_id"] if run["input"]["capability"] == "code_review"
                else run["native_job_ref"]["agent_space_id"])

    def capture_inventory(self, event):
        selected = self.runs_by_capability[event["capability"]]
        space, jobs = self._space(selected), self._native("inventory", selected)
        path = Path(self.case["assessment_journal_file"] + ".event-before.json")
        content = (json.dumps({"event_id": event["event_id"], "scope": self.case["scope"],
                               "run_id": selected["run_id"], "space": space, "jobs": jobs,
                               "observed_at": datetime.now(UTC).isoformat()},
                              sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(content) > 65536:
            raise AssertionError("native event baseline exceeds private evidence bound")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        return space, jobs

    def native_jobs_for_event(self, run, baseline):
        space, previous = baseline
        if self._space(run) != space:
            raise AssertionError("event used a different provider space")
        current = self._native("inventory", run)
        old = {json.dumps(job, sort_keys=True) for job in previous}
        updated = {json.dumps(job, sort_keys=True) for job in current}
        if not old <= updated:
            raise AssertionError("native job inventory changed during event qualification")
        return [job for job in current if json.dumps(job, sort_keys=True) not in old]

    def _native(self, action, run):
        self.remaining()
        provider = self.case["provider"]
        scope = self.case["scope"]
        path = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                f"{quote(scope['project_id'], safe='')}/services/security/bindings/"
                f"{quote(run['input']['binding_id'], safe='')}")
        binding = self.read_api(path)
        if (run["scope"] != scope or binding["scope"] != scope
                or binding["binding_id"] != run["input"]["binding_id"]
                or binding["native_resource_ref"]["record_id"] != self._space(run)
                or (run["admission"].get("provider_identity") is not None
                    and run["admission"]["provider_identity"] != binding["provider_identity"])
                or any(binding["provider_identity"].get(field) != provider[field]
                       for field in ("account_id", "role_arn", "region"))):
            raise AssertionError("provider identity differs from selected scoped binding")
        return self.read_api(path.rsplit("/bindings/", 1)[0] + "/runs/" +
                             quote(run["run_id"], safe="") + "/native-readback/" + action)

    def read_sources(self, run, plan):
        return product_sources(run, self.api)

    def reviews(self, run, result, findings, native, plan):
        path = Path(plan["review_file"])
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.stat().st_mode & 0o077):
            raise AssertionError("independent rubric must be a private regular file")
        packet = json.loads(path.read_text())
        if (packet["run_id"] != run["run_id"] or packet["result_id"] != result["result_id"]
                or packet["native_job_ref"] != run["native_job_ref"]
                or not packet.get("rubric_revision") or not isinstance(packet.get("review_sample"), list)
                or not isinstance(packet.get("known_controls"), list)):
            raise AssertionError("independent rubric is not bound to this result and native job")
        native["review_rubric_revision"] = packet["rubric_revision"]
        native["review_sample"] = packet["review_sample"]
        native["known_controls"] = packet["known_controls"]
        return packet["reviews"]

    def deliver_event(self, event):
        self.remaining()
        target = urlsplit(event["url"])
        if (target.scheme != "https" or not target.hostname or target.username or target.password
                or target.query or target.fragment or not event.get("event_id")):
            raise AssertionError("event delivery target is not authorized")
        attempt_id = event["event_id"] + "-" + str(sum(entry["operation"] == "event_delivery"
                            and entry["status"] == "prepared" for entry in self.journal.read()))
        if sum(entry["status"] == "prepared" for entry in self.journal.read()) >= self.case["max_attempts"]:
            raise AssertionError("event delivery exceeds authorized attempt bound")
        self.journal.append({"attempt_id": attempt_id, "operation": "event_delivery", "status": "prepared",
                             "work_id": event["event_id"], "observed_at": str(time.time())})
        response = self.api.post(event["url"], json=event["body"], headers={
            "Authorization": "Bearer " + os.environ[event["token_env"]], "Idempotency-Key": event["event_id"]})
        response.raise_for_status()
        receipt = response.json()
        if response.status_code != 202 or receipt["event_id"] != event["event_id"]:
            raise AssertionError("event receipt does not match selected native event")
        self.journal.append({"attempt_id": attempt_id, "operation": "event_delivery", "status": "acknowledged",
                             "work_id": event["event_id"], "effect_id": receipt["run_id"],
                             "observed_at": str(time.time())})
        return receipt

    def read_event_receipt(self, event):
        response = self.api.get(event["receipt_url"], headers={
            "Authorization": "Bearer " + os.environ[event["token_env"]], "Cache-Control": "no-cache"})
        response.raise_for_status()
        return response.json()

    def verify_triggered_evidence(self, run, result, findings, event):
        native = self.native_job(run)
        verify_assessment(run, result, findings, native, self.read_sources(run, event),
                          self.reviews(run, result, findings, native, event))
