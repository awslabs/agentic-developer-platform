"""Protected Context transport. Private fixture supplies existing read, replay and consumer URLs."""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import httpx

from assessment_live import LiveAssessmentDriver
from clients import api_reader, read_assessment
from clients_setup import LiveSetupDriver
from evidence import AttemptJournal
from l5_services.publication.contributions import contribution_pages
from providers import verify_assessment


ROOT = Path(__file__).resolve().parents[4]


def validate_context_fixture(case, foreign_case):
    selection = case.get("context_reuse")
    if (not isinstance(selection, dict) or selection.get("authorized_replay") is not True
            or selection.get("scope") != case["scope"]
            or foreign_case["scope"]["tenant_id"] == case["scope"]["tenant_id"]
            or not all(selection.get(field) for field in (
                "run_id", "result_id", "publication_id", "sources", "query_receipt",
                "assessment_plan", "delegation_id", "foreign_product_id"))
            or selection["foreign_product_id"] == case["scope"]["product_id"]
            or type(selection.get("max_attempts")) is not int
            or not 3 <= selection["max_attempts"] <= case["max_attempts"]):
        raise ValueError("Context reuse requires authorized, pinned and bounded private targets")
    journal = Path(selection["journal_file"])
    if (not journal.is_absolute() or journal.is_relative_to(ROOT) or journal.exists()
            or not journal.parent.is_dir() or journal.parent.stat().st_mode & 0o077):
        raise ValueError("Context replay requires a fresh journal in a private directory")
    for name in ("context_url", "context_count_url", "agent_query_url",
                 "agent_read_url", "visibility_url", "event_audit_url"):
        address = urlsplit(selection[name])
        if (address.scheme != "https" or not address.hostname or address.username or address.password
                or address.query or address.fragment
                or (name == "context_url" and address.path not in ("", "/"))):
            raise ValueError("Context evidence endpoints must be exact HTTPS URLs")
    names = ("context_token_env", "agent_token_env", "agent_read_token_env",
             "visibility_token_env", "audit_token_env", "foreign_product_token_env", "revoked_token_env")
    if (any(not os.environ.get(selection[name]) for name in names)
            or len({os.environ[selection[name]] for name in names[:5]}) != 5
            or os.environ[selection["foreign_product_token_env"]] == os.environ[selection["context_token_env"]]
            or os.environ[selection["revoked_token_env"]] == os.environ[selection["context_token_env"]]):
        raise ValueError("Context, later agent, audit and denied-scope evidence need separate authorization")


class LiveContextDriver:
    def __init__(self, case, foreign_case):
        validate_context_fixture(case, foreign_case)
        self.case = case
        self.deadline = time.monotonic() + case["deadline_seconds"]
        self.journal = AttemptJournal(case["context_reuse"]["journal_file"])
        self.api = httpx.Client(base_url=case["api_url"], follow_redirects=False, timeout=20,
                                headers={"Authorization": "Bearer " + os.environ[case["api_token_env"]]})
        self.read_api = api_reader(self.api)
        self.assessment = object.__new__(LiveAssessmentDriver)
        self.assessment.case, self.assessment.deadline = case, self.deadline
        self.assessment.api, self.assessment.read_api = self.api, self.read_api
        self.assessment.setup = object.__new__(LiveSetupDriver)
        self.assessment.setup.case, self.assessment.setup.deadline = case, self.deadline

    def close(self):
        self.api.close()

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("Context qualification exceeded authorized deadline; retain private attempts")
        return left

    def request(self, method, url, token_env, *, body=None, headers=None):
        with httpx.Client(follow_redirects=False, timeout=min(20, self.remaining())) as client:
            return client.request(method, url, json=body, headers={
                "Authorization": "Bearer " + os.environ[token_env], **(headers or {})})

    def read_product(self, selection):
        self.remaining()
        return read_assessment(self.case["scope"], selection["run_id"], selection["result_id"], self.read_api)

    def verify_result(self, run, result, findings, plan):
        if (run["input"]["capability"] != plan["input"]["capability"]
                or run["input"]["binding_id"] != plan["input"]["binding_id"]
                or plan["input"]["sources"] != run["input"]["sources"]):
            raise AssertionError("Security result differs from independently selected assessment plan")
        native = self.assessment.native_job(run)
        sources = self.assessment.read_sources(run, plan)
        reviews = self.assessment.reviews(run, result, findings, native, plan)
        return verify_assessment(run, result, findings, native, sources, reviews)

    def read_publication(self, selection):
        self.remaining()
        scope = self.case["scope"]
        response = self.api.get(
            f"/v1/products/{quote(scope['product_id'], safe='')}/projects/{quote(scope['project_id'], safe='')}"
            f"/services/security/runs/{quote(selection['run_id'], safe='')}/publications/"
            + quote(selection["publication_id"], safe=""))
        response.raise_for_status()
        packet = response.json()
        if packet["run_id"] != selection["run_id"]:
            raise AssertionError("publication readback has another Security run identity")
        return packet["publication"]

    def read_receipt(self, selection):
        response = self.request("GET", selection["context_url"].rstrip("/") + "/internal/v1/context/l5-contributions/"
                                + quote(selection["publication_id"], safe=""), selection["context_token_env"],
                                headers={"X-L5-Delegation-ID": selection["delegation_id"]})
        response.raise_for_status()
        return response.json()

    def record(self, attempt_id, operation, status, work_id, key):
        self.journal.append({"attempt_id": attempt_id, "run_id": self.case["context_reuse"]["run_id"],
                             "operation": operation, "status": status, "work_id": work_id,
                             "idempotency_key": key, "observed_at": datetime.now(UTC).isoformat()})

    def replay_publication(self, publication, selection):
        if (publication["state"] != "published" or publication["payload"]["scope"] != self.case["scope"]
                or publication["publication_id"] != selection["publication_id"]
                or publication["payload"]["contribution_id"] != publication["publication_id"]
                or publication["destination"] != "product-context"):
            raise AssertionError("refuse replay of a non-owned or incomplete durable contribution")
        pages = contribution_pages(SimpleNamespace(payload=publication["payload"],
                                                    contribution_hash=publication["contribution_hash"]))
        if 2 * len(pages) + 1 > selection["max_attempts"]:
            raise AssertionError("Context replay and later query exceed the authorized attempt bound")
        key = (f"{self.case['scope']['tenant_id']}/{publication['result_id']}/"
               f"{publication['result_version']}/{publication['destination']}/{publication['schema_version']}")
        receipts = []
        for repeat in range(2):
            for index, page in enumerate(pages):
                page_key = key + (f"/pages/{index}" if len(pages) > 1 else "")
                self.record(f"replay-{repeat}-{index}", "context_replay", "prepared",
                            publication["publication_id"], page_key)
                response = self.request("POST", selection["context_url"].rstrip("/")
                                        + "/internal/v1/context/l5-contributions",
                                        selection["context_token_env"], body=page,
                                        headers={"Idempotency-Key": page_key,
                                                 "X-L5-Delegation-ID": selection["delegation_id"]})
                response.raise_for_status()
                if index < len(pages) - 1 and response.status_code != 202:
                    raise AssertionError("Context admitted an incomplete contribution page set")
                if index == len(pages) - 1:
                    if response.status_code not in {200, 201}:
                        raise AssertionError("Context replay was not durably admitted")
                    receipts.append(response.json())
                    if receipts[-1] != publication["receipt"]:
                        raise AssertionError("Context replay changed the durable contribution receipt")
                self.record(f"replay-{repeat}-{index}", "context_replay", "acknowledged",
                            publication["publication_id"], page_key)
        return receipts

    def later_agent_read(self, selection):
        self.record("later-query", "later_agent_query", "prepared", selection["publication_id"],
                    selection["query_receipt"])
        response = self.request("POST", selection["agent_query_url"], selection["agent_token_env"],
                                body={"product_id": self.case["scope"]["product_id"],
                                      "project_id": self.case["scope"]["project_id"]},
                                headers={"Idempotency-Key": selection["query_receipt"]})
        response.raise_for_status()
        agent_run_id = response.json()["agent_run_id"]
        if not agent_run_id or agent_run_id == selection["run_id"]:
            raise AssertionError("later agent did not create a separate run")
        self.record("later-query", "later_agent_query", "acknowledged", agent_run_id,
                    selection["query_receipt"])
        response = self.request("GET", selection["agent_read_url"], selection["agent_read_token_env"])
        response.raise_for_status()
        observation = response.json()
        if observation["agent_run_id"] != agent_run_id:
            raise AssertionError("independent later agent readback differs from queried run")
        response = self.request("GET", selection["context_count_url"], selection["context_token_env"])
        response.raise_for_status()
        count = response.json()
        if (count["scope"] != self.case["scope"] or count["contribution_id"] != selection["publication_id"]
                or observation["consumer_count"] != count["count"]):
            raise AssertionError("independent Context contribution count differs from agent read")
        return observation

    def visibility(self, selection):
        response = self.request("GET", selection["visibility_url"], selection["visibility_token_env"])
        response.raise_for_status()
        return response.json()

    def denied_reads(self, selection, foreign_case):
        target = (selection["context_url"].rstrip("/") + "/internal/v1/context/l5-contributions/"
                  + quote(selection["publication_id"], safe=""))
        probes = []
        for name, token_env in (("foreign_product", selection["foreign_product_token_env"]),
                                ("revoked_access", selection["revoked_token_env"])):
            response = self.request("GET", target, token_env)
            body = response.json()
            detail = body.get("detail") if isinstance(body, dict) else None
            allowed = (isinstance(body, dict) and set(body) == {"detail"} and (
                detail in ("Not Found", "Forbidden") if isinstance(detail, str) else
                isinstance(detail, dict) and set(detail) == {"code"} and
                detail["code"] in {"not_found", "forbidden", "access_denied"}))
            serialized = json.dumps(body, sort_keys=True)
            disclosed = [value for value in (selection["result_id"], selection["publication_id"],
                         self.case["scope"]["tenant_id"], foreign_case["scope"]["tenant_id"])
                         if value in serialized]
            if not allowed:
                disclosed.append("unexpected_denial_payload")
            probes.append({"case": name, "status": response.status_code, "disclosed_resources": disclosed})
        return probes

    def event_audit(self, selection):
        response = self.request("GET", selection["event_audit_url"], selection["audit_token_env"])
        response.raise_for_status()
        packet = response.json()
        if packet["run_id"] != selection["run_id"] or packet["scope"] != self.case["scope"]:
            raise AssertionError("independent event audit has another scope or origin")
        return packet["audit"]
