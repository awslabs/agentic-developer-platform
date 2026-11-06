"""Original two-tenant setup with protected local human evidence and no browser.

Fixture tokens perform only their ordinary API/CLI/MCP calls. All human actions
happen in ADP under a separate current login. Private references are hints for a
fresh protected import, never authoritative evidence loaded from a file.
"""

import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from clients_setup import CliOperationError, LiveSetupDriver, McpOperationError

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from qualification_human_observations import digest, request_export
from release_context import release_identity

PATH = "/orchestration/operations-release-job/security-evidence"
FLAGS = {"release_check_completed": False, "native_qualified": False}


@dataclass(frozen=True, init=False)
class VerifiedManualSetupEvidence:
    """Expose comparison views only after a fresh protected Gateway import."""

    facts: dict

    @classmethod
    def from_import(cls, exported, *, selected, release, fixture_sha256, ticket_id):
        if (exported.get("contract") != "security-manual-setup-export/v1"
                or any(exported.get(key) is not value for key, value in FLAGS.items())):
            raise ValueError("manual evidence is not a protected partial import")
        facts = exported["facts"]
        request = facts["request"]
        current = request["release"]
        if (request["fixture_sha256"] != fixture_sha256 or request["selection"] != selected
                or any(current.get(key) != release[key] for key in ("correlation", "source_revision", "contract_digest", "run_id", "run_attempt"))
                or current["dispatch_revision"] != release["definition_revision"]
                or facts["grant"]["request"] != request
                or any(facts.get(key) is not value for key, value in FLAGS.items())):
            raise ValueError("protected import differs from the original release, fixture or owner consent")
        kinds = [item["kind"] for item in facts["events"]]
        valid = ({"prepared"} if selected["phase"] == "readiness" else {"observed"} if selected["phase"] == "display"
                 else {"effect_before", "effect_committed", "effect_reconciled" if "effect_reconciled" in kinds else "effect_after"})
        if len(kinds) != len(valid) or set(kinds) != valid:
            raise ValueError("manual evidence has duplicate or missing original effects")
        for item in facts["events"]:
            if (item["receipt"]["tenant_id"] != selected["tenant_id"]
                    or item["receipt"]["sha256"] != digest({"request_id": facts["origin_request_id"], "actor_id": item["actor_id"],
                        "actor_kind": item["actor_kind"], "document": item["document"]})
                    or any(item["document"].get(key) is not value for key, value in FLAGS.items())):
                raise ValueError("retained event digest or scope differs")
        reader = facts["grant"]["credentials"]["api"]
        expected = digest({"request_id": ticket_id, "actor_id": reader["user_id"], "actor_kind": "service",
            "document": {"contract": "security-manual-setup/v1", "reader": reader, "facts": facts, **FLAGS}})
        if exported["receipt"]["tenant_id"] != selected["tenant_id"] or exported["receipt"]["sha256"] != expected:
            raise ValueError("protected export digest or reader scope differs")
        instance = object.__new__(cls)
        object.__setattr__(instance, "facts", facts)
        return instance


def private_events(path):
    if not path.exists():
        return []
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 1048576:
            raise ValueError("manual Security references must be a bounded private file")
        with os.fdopen(descriptor, closefd=False) as stream:
            return [json.loads(line) for line in stream]
    finally:
        os.close(descriptor)


def retain(path, value):
    private_events(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def verify_fixture(fixture, filename, environment):
    identity = release_identity(environment)
    digest_value = hashlib.sha256(Path(filename).read_bytes()).hexdigest()
    if (identity["service"] != "security" or not identity["correlation"]
            or digest_value != environment["L5_FIXTURE_DIGEST"]
            or fixture["authorization"]["source_sha"] != identity["source_revision"]
            or fixture["release"]["worker"] != identity["source_revision"]
            or len(fixture["cases"]) != 2
            or len({case["scope"]["tenant_id"] for case in fixture["cases"]}) != 2
            or len({case["scope"]["product_id"] for case in fixture["cases"]}) != 2):
        raise ValueError("manual setup requires the original frozen two-tenant fixture and current release")
    expiry = datetime.fromisoformat(fixture["authorization"]["expires_at"].replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry <= datetime.now(UTC):
        raise ValueError("original fixture authorization expired")
    gateway = urlsplit(environment["ADP_GATEWAY_BASE_URL"])
    if any(urlsplit(case["api_url"]).netloc != gateway.netloc for case in fixture["cases"]):
        raise ValueError("fixture targets differ from the original Gateway")
    return identity, digest_value, expiry


class ManualSetupDriver(LiveSetupDriver):
    def __init__(self, case, fixture, filename, *, environment=None, readback=False):
        self.environment = dict(os.environ if environment is None else environment)
        self.release, self.fixture_sha256, self.fixture_expiry = verify_fixture(fixture, filename, self.environment)
        bounded = {**case, "deadline_seconds": fixture["limits"]["deadline_seconds"],
                   "max_attempts": fixture["limits"]["max_attempts"]}
        super().__init__(bounded, None, existing_journal=True)
        self.manual_file = Path(case["journal_file"] + ".manual.jsonl")
        self.references = private_events(self.manual_file)
        if any(item["release"] != self.release or item["fixture_sha256"] != self.fixture_sha256 for item in self.references):
            raise ValueError("retained setup references belong to another release or fixture")
        self.fixture_cases = [{key: case["scope"][key] for key in ("tenant_id", "product_id", "project_id")}
                              | {key: case["selected"][key] for key in ("binding_id", "configuration_id")}
                              for case in fixture["cases"]]
        self.readback = readback
        self.deadline = min(self.deadline, time.monotonic() + (self.fixture_expiry - datetime.now(UTC)).total_seconds())
        if not readback:
            started = next((item["started_at"] for item in self.references if item.get("kind") == "start"), None)
            if started is not None:
                left = (datetime.fromisoformat(started) - datetime.now(UTC)).total_seconds() + bounded["deadline_seconds"]
                self.deadline = min(self.deadline, time.monotonic() + left)
            else:
                self.retain(kind="start", started_at=datetime.now(UTC).isoformat())

    def retain(self, **data):
        value = {"release": self.release, "fixture_sha256": self.fixture_sha256, **data}
        retain(self.manual_file, value)
        self.references.append(value)

    def product_call(self, client, suffix, body=None):
        response = client.request("GET" if body is None else "POST", self.path + "/release-evidence/" + suffix,
                                  json=body, timeout=self.remaining())
        response.raise_for_status()
        return response.json()

    def selected(self, phase, run_id=None):
        configuration = self.read_api(self.path + "/configuration")
        binding = self.read_api(self.path + "/bindings/" + quote(self.case["selected"]["binding_id"], safe=""))
        selected = {**{key: self.case["scope"][key] for key in ("tenant_id", "product_id", "project_id")},
            "binding_id": binding["binding_id"], "configuration_id": configuration["configuration_id"],
            "configuration_revision": configuration["revision"], "configuration_hash": configuration["hash"],
            "binding_revision": binding["revision"], "binding_generation": binding["generation"],
            "source_sha256": digest(configuration["settings"]["source_refs"]),
            "connection_sha256": digest(binding["connection_ref"]), "provider_sha256": digest(binding["provider_identity"]),
            "observer_id": None, "phase": phase, "ui_region": self.case["ui_region"],
            "ui_reason": self.case["ui_reason"], "ui_receipt": self.case["ui_receipt"],
            "run_id": None, "run_revision": None, "run_input_sha256": None}
        if run_id:
            run = self.read_api(self.path + "/runs/" + quote(run_id, safe=""))
            selected.update(run_id=run_id, run_revision=run["revision"], run_input_sha256=digest(run["input"]))
        if (configuration["configuration_id"] != self.case["selected"]["configuration_id"]
                or binding["connection_ref"] != self.case["selected"]["connection_ref"]
                or binding["provider_identity"] != self.case["selected"]["provider_identity"]):
            raise ValueError("manual selection differs from the original fixture")
        return selected

    def issue(self, selected, renew_of=None):
        self.remaining()
        if self.deadline - time.monotonic() < 30:
            raise ValueError("less than 30 seconds remain in the original human window; preserve receipts for read-only reconciliation")
        prior = next((item for item in reversed(self.references) if item.get("kind") == "request"
                      and item["selection"] == selected), None)
        if prior and renew_of is None:
            self.register_credentials(prior)
            return prior
        response = request_export(self.environment, self.release, {"fixture_sha256": self.fixture_sha256,
            "fixture_cases": self.fixture_cases, "fixture_expires_at": self.fixture_expiry.isoformat(),
            "selections": [selected], "lifetime_seconds": max(30, min(300, int(self.deadline - time.monotonic()))),
            **({"renew_of": {"request_id": renew_of["request_id"], "tenant_id": renew_of["tenant_id"]}} if renew_of else {})}, path=PATH + "/requests")
        request = response["requests"][0]
        if request["request"]["selection"] != selected or request["tenant_id"] != selected["tenant_id"]:
            raise ValueError("Gateway issued another selection")
        value = {"kind": "request", "selection": selected, "request_id": request["request_id"], "tenant_id": selected["tenant_id"]}
        self.retain(**value)
        self.register_credentials(value)
        print(f"Security {selected['phase']} awaits Product owner/observer in ADP; request {request['request_id']}. No browser automation.", flush=True)
        return value

    def register_credentials(self, reference):
        state = self.product_call(self.api, "requests/" + reference["request_id"])
        if state["status"] in {"expired", "revoked"}:
            return
        # Revalidate every actual current credential even when its slot exists.
        # The local endpoint replays the exact proof after consent and rejects a
        # changed token; a restart cannot silently rotate automation identities.
        for slot, client in (("api", self.api), ("mcp", self.mcp)):
            self.product_call(client, "requests/" + reference["request_id"] + "/credentials", {"slot": slot})
        with httpx.Client(base_url=self.case["api_url"], follow_redirects=False, timeout=self.remaining(),
                          headers={"Authorization": "Bearer " + os.environ[self.case["ui_access_token_env"]]}) as legacy:
            self.product_call(legacy, "requests/" + reference["request_id"] + "/credentials", {"slot": "legacy_ui"})
        self.cli("evidence-register", resource=("request_id", reference["request_id"]))

    def collect(self, selected):
        reference = self.issue(selected)
        while True:
            self.remaining()
            state = self.product_call(self.api, "requests/" + reference["request_id"])
            kinds = {item["kind"] for item in state["receipts"]}
            required = "prepared" if selected["phase"] == "readiness" else "observed" if selected["phase"] == "display" else "effect_after"
            if selected["phase"] == "edit" and "effect_before" in kinds and "effect_reconciled" not in kinds:
                if "effect_after" not in kinds or any(item["kind"] == "effect_after" and item["document"]["outcome"] == "unknown" for item in state["events"]):
                    try:
                        self.product_call(self.api, "requests/" + reference["request_id"] + "/reconcile", {})
                    except httpx.HTTPStatusError as error:
                        try:
                            code = error.response.json()["detail"]["code"]
                        except (ValueError, KeyError, TypeError):
                            raise error
                        if error.response.status_code != 409 or code not in {
                                "security_evidence_pending", "security_evidence_effect_outcome_unknown"}:
                            raise
                        # The independently committed before marker can be seen
                        # while the single canonical save is still in flight.
                        # Retry only this read-only reconciliation, bounded by
                        # the original deadline; never repeat the save.
                        time.sleep(min(2, self.remaining()))
                        continue
                    kinds.add("effect_reconciled")
            if state["status"] == "expired":
                reference = self.issue(selected, renew_of=reference)
                continue
            if state["request"].get("export_only") and "grant" in kinds:
                break
            if required in kinds or selected["phase"] == "edit" and "effect_reconciled" in kinds:
                break
            if state["status"] in {"expired", "revoked"}:
                raise ValueError("Security human request expired or was revoked; renew its exact request before continuing")
            time.sleep(min(2, self.remaining()))
        selector = {"tenant_id": reference["tenant_id"], "request_id": reference["request_id"]}
        ticket = request_export(self.environment, self.release, selector, path=PATH + "/tickets")
        self.product_call(self.api, "exports", {"ticket_id": ticket["ticket_id"]})
        exported = request_export(self.environment, self.release, {**selector, "ticket_id": ticket["ticket_id"]}, path=PATH + "/imports")
        evidence = VerifiedManualSetupEvidence.from_import(exported, selected=selected, release=self.release,
            fixture_sha256=self.fixture_sha256, ticket_id=ticket["ticket_id"])
        self.retain(kind="import", request_id=reference["request_id"], receipt=exported["receipt"])
        return evidence.facts

    def cli_update(self, settings_path, etag, receipt):
        settings = json.loads(Path(settings_path).read_text())
        self.retain(kind="cli_intent", receipt=receipt, if_match=etag,
                    request_sha256=digest({"settings": settings, "if_match": etag}))
        return super().cli_update(settings_path, etag, receipt)

    def reconcile_cli(self):
        intent = next((item for item in self.references if item.get("kind") == "cli_intent"), None)
        if intent is None or intent["receipt"] != self.case["cli_receipt"]:
            raise ValueError("original conditional CLI intent is unavailable; preserve this attempt for explicit reconciliation")
        settings = json.loads(Path(self.case["cli_settings_file"]).read_text())
        if intent["request_sha256"] != digest({"settings": settings, "if_match": intent["if_match"]}):
            raise ValueError("original CLI payload or condition changed")
        try:
            return self.cli("evidence-receipt", resource=("receipt_id", self.case["cli_receipt"]))["detail"]
        except CliOperationError as error:
            if error.status_code != 404:
                raise
        count = sum(item.get("kind") == "cli_idempotent_replay" for item in self.references)
        if count >= self.case["max_attempts"]:
            raise ValueError("original CLI receipt remains unavailable within bounded attempts")
        self.retain(kind="cli_idempotent_replay", receipt=intent["receipt"], request_sha256=intent["request_sha256"])
        # Only an absent receipt permits the same conditional request. If R has
        # changed the canonical If-Match refuses it; no new baseline is read.
        self.cli("config", "update", input_file=self.case["cli_settings_file"],
                 match=intent["if_match"], receipt=intent["receipt"])
        return self.cli("evidence-receipt", resource=("receipt_id", intent["receipt"]))["detail"]

    def ui_edit(self, region, reason):
        if (region, reason) != (self.case["ui_region"], self.case["ui_reason"]):
            raise ValueError("requested UI choices changed")
        previous = self.journal.read() if self.journal.path.exists() else []
        if not any(item["operation"] == "ui_edit" for item in previous):
            self.record(self.case["ui_receipt"], "ui_edit", "prepared")
        selected = self.selected("edit")
        # The exact pre-save selection is retained for read-only reconciliation.
        prior = next((item for item in reversed(self.references) if item.get("kind") == "request" and item["selection"]["phase"] == "edit"), None)
        if prior:
            selected = prior["selection"]
        facts = self.collect(selected)
        after = facts["events"][-1]["document"]["after"]["configuration"]
        if (after["revision"] != selected["configuration_revision"] + 1
                or after["settings"]["region"] != region):
            raise ValueError("manual UI save did not produce the original requested revision")
        if not any(item["operation"] == "ui_edit" and item["status"] == "acknowledged" for item in previous):
            self.record(self.case["ui_receipt"], "ui_edit", "acknowledged", after["configuration_id"], self.case["ui_receipt"])
        return after

    def wait_for_execution_consent(self):
        announced = False
        while True:
            try:
                self.verify_provider_authority(self.case["provider"])
                return
            except httpx.HTTPStatusError as error:
                try:
                    code = error.response.json()["detail"]["code"]
                except (ValueError, KeyError, TypeError):
                    raise error
                if code not in {"delegation_denied", "delegation_scope_changed"}:
                    raise
                if not announced:
                    print(f"Security tenant {self.case['scope']['tenant_id']}: the Product owner must review the current configuration and authorize its exact native execution scope after the CLI/UI edits. No assessment has been submitted.", flush=True)
                    announced = True
                time.sleep(min(2, self.remaining()))

    def mcp_submit(self, run_input, receipt):
        if receipt != self.case["mcp_receipt"]:
            raise ValueError("MCP receipt differs from the original fixture")
        input_hash = digest(run_input)
        intents = [item for item in self.references if item.get("kind") in {"mcp_intent", "mcp_idempotent_replay", "mcp_source_preparing"}]
        if any(item["receipt"] != receipt or item["input_sha256"] != input_hash for item in intents):
            raise ValueError("MCP input differs from the retained original intent")
        previous = self.journal.read() if self.journal.path.exists() else []
        acknowledged = next((item for item in previous if item["operation"] == "mcp_submit" and item["status"] == "acknowledged"), None)
        started = any(item["operation"] == "mcp_submit" for item in previous)
        preparing = any(item.get("kind") == "mcp_source_preparing" for item in intents)

        def submit_original():
            nonlocal preparing
            try:
                return self.mcp_call("l5___submit_run", {**self._scope(), "input": run_input,
                                                         "idempotency_key": receipt}, status=202)
            except McpOperationError as error:
                if error.status_code != 503 or error.code != "source_capture_preparing":
                    raise
                if not preparing:
                    self.retain(kind="mcp_source_preparing", receipt=receipt, input_sha256=input_hash)
                preparing = True
                return None

        def receipt_timeout():
            try:
                return self.remaining()
            except AssertionError as error:
                if preparing:
                    raise AssertionError("Source preparation receipt did not appear before the original authorized deadline; "
                                         "inspect source preparation for the original request and preserve its receipt key") from error
                raise

        if not started:
            self.wait_for_execution_consent()
            # Admission/journal assertions must never fall through to replay.
            self.record(receipt, "mcp_submit", "prepared")
            self.retain(kind="mcp_intent", receipt=receipt, input_sha256=input_hash)
            try:
                result = submit_original()
            except httpx.TransportError:
                result = None
        else:
            result = None
        response = self.mcp.get(self.path + "/runs/receipts/" + quote(receipt, safe=""), timeout=receipt_timeout())
        if response.status_code == 404 and not preparing:
            replays = sum(item.get("kind") == "mcp_idempotent_replay" for item in self.references)
            if replays >= self.case["max_attempts"]:
                raise ValueError("original MCP receipt remains unavailable within the bounded reconciliation attempts")
            self.wait_for_execution_consent()
            self.retain(kind="mcp_idempotent_replay", receipt=receipt, input_sha256=digest(run_input))
            # The Product idempotency key owns the initial run. Replaying that
            # exact request may create its missing receipt, never a second run.
            result = submit_original()
            response = self.mcp.get(self.path + "/runs/receipts/" + quote(receipt, safe=""), timeout=receipt_timeout())
        while response.status_code == 404 and preparing:
            # Durable source preparation owns the original submission now.
            # Poll only its receipt; restart cannot extend the original deadline.
            time.sleep(min(2, receipt_timeout()))
            response = self.mcp.get(self.path + "/runs/receipts/" + quote(receipt, safe=""), timeout=receipt_timeout())
        response.raise_for_status()
        recovered = response.json()
        run = self.read_api(self.path + "/runs/" + quote(recovered["run_id"], safe=""))
        if run["input"] != run_input or result is not None and result["run_id"] != run["run_id"]:
            raise ValueError("initial MCP receipt differs from the original pinned input")
        if acknowledged is not None and acknowledged["effect_id"] != run["run_id"]:
            raise ValueError("retained MCP run changed")
        entries = self.journal.read()
        if not any(item["operation"] == "mcp_submit" and item["status"] == "acknowledged" for item in entries):
            self.record(receipt, "mcp_submit", "acknowledged", run["run_id"], receipt)
        return result or run

    def ui_observations(self, binding_id, run_id):
        if binding_id != self.case["selected"]["binding_id"]:
            raise ValueError("manual binding changed")
        facts = self.collect(self.selected("display", run_id))
        observation = facts["events"][0]["document"]["observation"]
        if observation["observation"] != "displayed":
            raise AssertionError("selected human reported a UI mismatch or denial")
        return facts["grant"]["snapshot"]

    def run_setup(self, limits):
        from clients import verify_setup
        from scenarios import clients_and_setup

        entries = self.journal.read() if self.journal.path.exists() else []
        if not entries:
            return clients_and_setup(self.case, limits, self)
        operations = {item["operation"] for item in entries}
        if "cli_update" not in operations or operations - {"cli_update", "ui_edit", "mcp_submit"}:
            raise ValueError("retained setup attempts do not match the original operation sequence")
        receipt = self.reconcile_cli()
        settings = json.loads(Path(self.case["cli_settings_file"]).read_text())
        if (receipt["request_sha256"] != digest({"settings": settings, "if_match": '"' + receipt["before_hash"] + '"'})
                or receipt["configuration"]["configuration_id"] != self.case["selected"]["configuration_id"]
                or receipt["configuration"]["settings"]["region"] != self.case["cli_region"]):
            raise ValueError("original CLI edit receipt differs from the selected request")
        if not any(item["operation"] == "cli_update" and item["status"] == "acknowledged" for item in entries):
            self.record(self.case["cli_receipt"], "cli_update", "acknowledged", receipt["configuration"]["configuration_id"], self.case["cli_receipt"])
        edited = self.ui_edit(self.case["ui_region"], self.case["ui_reason"])
        if edited["revision"] != receipt["configuration"]["revision"] + 1:
            raise ValueError("reconciled UI save is not the revision after the original CLI save")
        submitted = self.mcp_submit({**self.case["run_input"], "configuration_revision": edited["revision"]}, self.case["mcp_receipt"])
        selection = {**self.case["selected"], "run_id": submitted["run_id"], "max_attempts": limits["max_attempts"],
            "effects": {"cli_update": edited["configuration_id"], "ui_edit": edited["configuration_id"], "mcp_submit": submitted["run_id"]}}
        observations = {"cli": self.cli_observations(selection["binding_id"], submitted["run_id"]),
            "ui": self.ui_observations(selection["binding_id"], submitted["run_id"]),
            "mcp": self.mcp_observations(selection["binding_id"], submitted["run_id"])}
        return verify_setup(selection, observations, self.attempts(selection), self.read_api)


def preflight(drivers):
    if len(drivers) != 2 or len({driver.case["scope"]["tenant_id"] for driver in drivers}) != 2:
        raise ValueError("both original distinct tenants are required before any setup mutation")
    selections = []
    for driver in drivers:
        tenant = driver.case["scope"]["tenant_id"]
        identity = driver.product_call(driver.api, "identity")
        if identity["user_id"] != identity["owner_id"]:
            raise ValueError(f"Security tenant {tenant}: the fixture API principal is not the current Product owner required by provider and run operations. Establish explicitly authorized owner fixture transport and fresh native consent; do not impersonate or reuse transferred-owner authority.")
        if identity["human_owner_eligibility"] != "available":
            raise ValueError(f"Security tenant {tenant}: the Product owner is a designated automation account. A legitimate human owner with current local membership must approve a fresh request before setup.")
        try:
            selected = driver.selected("readiness")
            driver.verify_provider_authority(driver.case["provider"])
        except httpx.HTTPStatusError as error:
            try:
                code = error.response.json()["detail"]["code"]
            except (ValueError, KeyError, TypeError):
                code = "provider_unavailable"
            if code in {"delegation_denied", "delegation_scope_changed"}:
                raise ValueError(f"Security tenant {tenant}: the actual Product owner must authorize the current native execution scope before provider verification. Configuration edits will require fresh explicit consent before MCP submission.") from error
            raise ValueError(f"Security tenant {tenant}: verify current Product source/provider access ({code}) before setup.") from error
        except (httpx.HTTPError, AssertionError, ValueError, KeyError) as error:
            raise ValueError(f"Security tenant {tenant}: original Product source/profile, Security configuration, binding and provider connection must be ready and match the frozen fixture before setup.") from error
        # Fresh readiness never rewrites the original CLI intent or its R -> R+1
        # receipt. Both owners must approve the current source/provider snapshot
        # before either tenant can continue a retained setup attempt.
        selections.append(selected)
    for driver, selected in zip(drivers, selections):
        driver.issue(selected)
    for driver, selected in zip(drivers, selections):
        driver.collect(selected)
