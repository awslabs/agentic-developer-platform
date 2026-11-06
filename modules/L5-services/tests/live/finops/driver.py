"""Bounded, readback-first Product path for a private FinOps qualification run."""

import hashlib
from datetime import UTC, datetime, timedelta
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from evaluator import evaluate, matches_scope

from l5_services.run_input import FinopsRunInput, validate_run_input


ROOT = Path(__file__).resolve().parents[5]
RECEIPT = re.compile(r"[A-Za-z0-9_-]{16,128}\Z")


class SetupInquiry(FinopsRunInput):
    configuration_revision: None = None
    binding_id: None = None


def session_token(manifest, key):
    if manifest.get("session_file"):
        path = private_path(manifest["session_file"])
        if path.stat().st_size > 65536:
            raise ValueError("private Product sessions are oversized")
        tokens = json.loads(path.read_text())
        value = tokens.get(key) if isinstance(tokens, dict) else None
    else:
        name = manifest.get(key)
        value = os.environ.get(name) if isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]+", name) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("qualification requires selected private tokens")
    return value


def private_path(value, *, directory=False):
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("qualification requires an existing protected private path")
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or path.resolve().is_relative_to(ROOT)
            or not (path.is_dir() if directory else path.is_file())
            or path.stat().st_mode & 0o077):
        raise ValueError("qualification requires an existing protected private path")
    return path


def validate(manifest):
    if not isinstance(manifest, dict) or manifest.get("authorized") is not True:
        raise ValueError("qualification requires explicit private authorization")
    source_sha = manifest.get("source_sha", manifest.get("release_sha"))
    if manifest.get("release_sha", source_sha) != source_sha:
        raise ValueError("FinOps fixture has conflicting release revisions")
    manifest["release_sha"] = source_sha
    try:
        expiry = datetime.fromisoformat(manifest["expires_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        raise ValueError("qualification requires an aware, expiring authorization") from None
    if (expiry.tzinfo is None or not datetime.now(UTC) < expiry <= datetime.now(UTC) + timedelta(hours=24)
            or not isinstance(manifest.get("release_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{40}", manifest["release_sha"])):
        raise ValueError("qualification requires a bounded authorization and pinned release")
    scope = manifest.get("scope")
    if (not isinstance(scope, dict) or set(scope) != {"tenant_id", "product_id", "project_id"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[\w-]{1,128}", value)
                   for value in scope.values())):
        raise ValueError("qualification requires one Product and project")
    url = urlsplit(manifest.get("gateway_url", ""))
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path not in ("", "/")):
        raise ValueError("qualification requires an exact HTTPS gateway origin")
    request = manifest.get("input")
    if "bootstrap" in manifest:
        from bootstrap import validate_bootstrap

        SetupInquiry.model_validate(request)
        validate_bootstrap(manifest)
    else:
        validate_run_input(request)
    if (request.get("service_id") != "finops" or request.get("capability") != "cost_inquiry"
            or request.get("binding_id") != manifest.get("binding_id")
            or request.get("configuration_revision") != manifest.get("configuration_revision")):
        raise ValueError("native inquiry differs from the selected Product binding")
    label = manifest.get("native_agent_display_name")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("selected native agent label needs Product authority")
    agent = manifest.get("native_agent_ref")
    billing = manifest.get("billing_scope")
    if (not isinstance(agent, dict) or agent.get("tenant_id") != scope["tenant_id"]
            or agent.get("project_id") != scope["project_id"]
            or agent.get("record_type") != "finops_agent" or not agent.get("record_id")
            or not agent.get("record_revision") or not isinstance(billing, dict)
            or not isinstance(billing.get("view_ref"), str) or not billing["view_ref"]
            or not isinstance(billing.get("account_ids"), list) or not billing["account_ids"]):
        raise ValueError("native agent and billing view must be pinned to a Product binding")
    for key in ("receipt", "cleanup_receipt"):
        if not isinstance(manifest.get(key), str) or RECEIPT.fullmatch(manifest[key]) is None:
            raise ValueError("qualification needs distinct durable receipts")
    if manifest["receipt"] == manifest["cleanup_receipt"]:
        raise ValueError("cleanup cannot reuse the native submission receipt")
    for key in ("api_token_env", "mcp_token_env", "readback_token_env"):
        session_token(manifest, key)
    if (type(manifest.get("max_requests")) is not int or not 1 <= manifest["max_requests"] <= 50
            or type(manifest.get("deadline_seconds")) is not int or not 1 <= manifest["deadline_seconds"] <= 600):
        raise ValueError("qualification must bound requests and elapsed time")
    executable = private_path(manifest.get("cli_executable"))
    private_path(manifest.get("cli_home"), directory=True)
    if "cli_config_dir" in manifest:
        private_path(manifest["cli_config_dir"], directory=True)
    private_path(manifest.get("browser_storage_state"))
    if (not os.access(executable, os.X_OK)
            or hashlib.sha256(executable.read_bytes()).hexdigest() != manifest.get("cli_sha256")
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", manifest.get("cli_deployment", ""))):
        raise ValueError("installed CLI is not pinned to the selected deployment")
    frontend = urlsplit(manifest.get("frontend_url", ""))
    if (frontend.scheme != "https" or not frontend.hostname or frontend.username or frontend.password
            or frontend.query or frontend.fragment or frontend.path not in ("", "/")):
        raise ValueError("first-party UI needs a selected HTTPS origin")
    return manifest


class FinOpsDriver:
    def __init__(self, manifest, directory, *, client=None, mode="run"):
        if mode not in {"run", "preflight", "readback", "setup"}:
            raise ValueError("unsupported FinOps qualification mode")
        self.mode = mode
        self.manifest = validate(manifest)
        self.directory = private_path(directory, directory=True)
        self.path = (f"/api/v1/products/{quote(manifest['scope']['product_id'], safe='')}/projects/"
                     f"{quote(manifest['scope']['project_id'], safe='')}/services/finops")
        self.client = client or httpx.Client(follow_redirects=False)
        expiry = datetime.fromisoformat(manifest["expires_at"].replace("Z", "+00:00"))
        self.deadline = time.monotonic() + min(manifest["deadline_seconds"],
                                               (expiry - datetime.now(UTC)).total_seconds())
        self.calls = 0
        self.submitted = False
        self.owned_run_id = None
        self.client_observations = {}
        self.journal_path = self.directory / f"finops-{manifest['receipt']}-actions.jsonl"
        if mode == "run":
            descriptor = os.open(self.journal_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        elif mode == "readback":
            journal = private_path(self.journal_path)
            if journal.stat().st_size > 65536:
                raise ValueError("FinOps qualification journal is oversized")
            entries = [json.loads(line) for line in journal.read_text().splitlines()]
            if (len(entries) != 3 or any(not isinstance(entry, dict) for entry in entries)
                    or [entry.get("state") for entry in entries] != ["prepared", "acknowledged", "qualification_observed"]
                    or any(entry.get("receipt") != manifest["receipt"] for entry in entries)
                    or not entries[1].get("run_id") or entries[2].get("run_id") != entries[1]["run_id"]):
                raise ValueError("FinOps qualification journal lacks an observed original run")
            self.owned_run_id = entries[1]["run_id"]
            self.submitted = True

    def close(self):
        self.client.close()

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise ValueError("native qualification deadline expired; reconcile original receipt")
        return seconds

    def journal(self, state, run_id=None):
        if self.mode != "run":
            raise ValueError("read-only qualification cannot change the action journal")
        descriptor = os.open(self.journal_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "a", encoding="utf-8") as destination:
            destination.write(json.dumps({"state": state, "receipt": self.manifest["receipt"],
                                          "run_id": run_id}, sort_keys=True) + "\n")
            destination.flush()
            os.fsync(destination.fileno())

    def request(self, method, path, *, token="readback_token_env", body=None, receipt=None, status=None,
                max_bytes=65536, preconditions=None):
        mcp_readback = (method == "POST" and path == "/api/v1/l5/mcp/tools/call"
                        and isinstance(body, dict) and body.get("tool") == "l5___get_result")
        if self.mode not in {"run", "setup"} and method != "GET" and not mcp_readback:
            raise ValueError("read-only qualification cannot submit Product work")
        self.calls += 1
        if self.calls > self.manifest["max_requests"]:
            raise ValueError("native qualification request limit reached")
        response = self.client.request(method, self.manifest["gateway_url"].rstrip("/") + path,
                                       json=body, headers={**(preconditions or {}),
                                                           "Authorization": "Bearer " + session_token(self.manifest, token),
                                                           **({"Idempotency-Key": receipt} if receipt else {})},
                                       timeout=min(self.remaining(), 15))
        if response.status_code != (status or (202 if method == "POST" else 200)) or len(response.content) > max_bytes:
            raise ValueError("scoped Product request denied or oversized; do not retry paid work")
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Product authority returned an invalid observation")
        return result

    def check_cli_target(self):
        selected = self.manifest
        environment = self.client_environment()
        resolver = Path(selected["cli_executable"]).with_name("adp_deployments.py")
        response = subprocess.run([sys.executable, "-I", str(resolver), "resolve",
                                   "--deployment", selected["cli_deployment"], "--format", "json"],
                                  capture_output=True, env=environment, timeout=min(self.remaining(), 20))
        if response.returncode or len(response.stdout) > 65536:
            raise ValueError("installed CLI deployment cannot be independently resolved")
        metadata = json.loads(response.stdout)
        if (not isinstance(metadata, dict) or metadata.get("deployment") != selected["cli_deployment"]
                or metadata.get("gateway_url", "").rstrip("/") != selected["gateway_url"].rstrip("/") + "/api"):
            raise ValueError("installed CLI points at a different Product gateway")

    def installation_preflight(self):
        self.check_cli_target()
        for token in ("api_token_env", "readback_token_env"):
            options = self.request("GET", self.path + "/configuration/options", token=token)
            if (options.get("service_id") != "finops" or "cost_inquiry" not in options.get("capabilities", [])
                    or not options.get("connections")
                    or set(options.get("unavailable_reasons", [])) - {
                        "region_selection_required", "billing_scope_unverified", "native_unqualified"}):
                raise ValueError("FinOps Product access prerequisites are unavailable")
            if "bootstrap" in self.manifest:
                settings = self.manifest["bootstrap"]["configuration"]
                if (settings.get("connection_ref") not in options["connections"]
                        or settings.get("region") not in options.get("regions", [])):
                    raise ValueError("FinOps setup connection or region is not authorized")
        return {"installation_prerequisites": "available", "native_qualification": "not_run"}

    def preflight(self):
        self.check_cli_target()
        selected = self.manifest
        scope = selected["scope"]
        config = self.request("GET", self.path + "/configuration")
        binding = self.request("GET", self.path + "/bindings/" + quote(selected["binding_id"], safe=""))
        settings = config.get("settings") or {}
        native = binding.get("native_resource_ref")
        config_scope = config.get("scope")
        if (not isinstance(config_scope, dict)
                or any(config_scope.get(field) != value for field, value in scope.items())
                or not config_scope.get("owner_id")
                or not isinstance(config_scope.get("relationship_revision"), str)
                or not config_scope["relationship_revision"]
                or not isinstance(config_scope.get("visibility"), str)
                or not config_scope["visibility"]
                or config.get("revision") != selected["configuration_revision"]
                or config.get("state") != "ready" or settings.get("enabled") is not True
                or any(((settings.get("finops") or {}).get("billing_scope") or {}).get(field)
                       != selected["billing_scope"].get(field) for field in ("view_ref", "account_ids"))
                or any((binding.get("billing_scope") or {}).get(field) != value
                       for field, value in selected["billing_scope"].items())
                or binding.get("scope") != config_scope or binding.get("binding_id") != selected["binding_id"]
                or binding.get("state") != "ready" or binding.get("configuration_id") != config.get("configuration_id")
                or binding.get("native_resource_ref") != selected["native_agent_ref"]
                or binding.get("native_agent_display_name") != selected["native_agent_display_name"]
                or not isinstance(native, dict) or native.get("tenant_id") != scope["tenant_id"]
                or native.get("project_id") != scope["project_id"]):
            raise ValueError("current Product configuration or native binding differs from authorized selection")
        return binding

    def submit_once(self):
        if self.submitted or self.mode != "run":
            raise ValueError("the authorized native receipt was already used")
        self.preflight()
        self.submitted = True
        self.journal("prepared")
        try:
            run = self.request("POST", self.path + "/runs", token="api_token_env",
                               body=self.manifest["input"], receipt=self.manifest["receipt"])
            if not matches_scope(run.get("scope"), self.manifest["scope"]) or not run.get("run_id"):
                raise ValueError("Product did not acknowledge the selected run")
            self.owned_run_id = run["run_id"]
            self.journal("acknowledged", run["run_id"])
            return run["run_id"]
        except Exception:
            self.journal("uncertain")
            raise

    def read_run(self, run_id):
        if run_id != self.owned_run_id or not self.submitted:
            raise ValueError("readback is not owned by the original native receipt")
        run = self.request("GET", self.path + "/runs/" + quote(run_id, safe=""))
        if (run.get("run_id") != run_id or not matches_scope(run.get("scope"), self.manifest["scope"])
                or run.get("input") != self.manifest["input"]
                or (run.get("admission") or {}).get("native_agent_ref") != self.manifest["native_agent_ref"]):
            raise ValueError("original Product inquiry differs from scoped readback")
        return run

    def read_result(self, run_id):
        run = self.read_run(run_id)
        if run.get("state") not in {"succeeded", "partial"}:
            raise ValueError("original native inquiry has not produced a terminal result")
        reference = (run.get("provider_readback_ref") or {}).get("answer_artifact")
        if (not isinstance(reference, dict) or not all(reference.get(field) for field in
                                                     ("artifact_id", "version", "sha256"))):
            raise ValueError("original native answer lacks versioned evidence")
        cursor = None
        for _ in range(5):
            suffix = "/results" + ("?cursor=" + quote(cursor, safe="") if cursor else "")
            page = self.request("GET", self.path + suffix)
            if not isinstance(page.get("items"), list):
                raise ValueError("scoped Product result listing is unavailable")
            for item in page["items"]:
                if item.get("run_id") != run_id:
                    continue
                if not isinstance(item.get("result_id"), str) or not re.fullmatch(r"[\w-]{1,128}", item["result_id"]):
                    raise ValueError("original Product result identity is invalid")
                result = self.request("GET", self.path + "/results/" + quote(item["result_id"], safe=""))
                native = result.get("native") or {}
                oracle = result.get("oracle") or {}
                if (not matches_scope(result.get("scope"), self.manifest["scope"]) or result.get("run_id") != run_id
                        or result.get("revision") != item.get("revision")
                        or result.get("query") != self.manifest["input"]["query"]
                        or any(result.get("billing_scope", {}).get(field) != self.manifest["billing_scope"][field]
                               for field in ("view_ref", "account_ids"))
                        or native.get("answer_artifact") != reference
                        or native.get("status") != (run.get("provider_readback_ref") or {}).get("status")
                        or not oracle.get("collection_id") or not oracle.get("pages")):
                    raise ValueError("native or independent billing result differs from original Product run")
                return result
            cursor = page.get("next_cursor")
            if not cursor:
                break
        raise ValueError("original native inquiry has no independently retrievable result")

    def _same_result(self, observed, result):
        if (not isinstance(observed, dict) or not matches_scope(observed.get("scope"), self.manifest["scope"])
                or observed.get("result_id") != result["result_id"]
                or observed.get("run_id") != result["run_id"]
                or observed.get("revision") != result["revision"]
                or observed.get("native") != result["native"]
                or observed.get("oracle") != result["oracle"]):
            raise ValueError("independent client saw another native or billing result")
        return observed

    def native_answer(self, result):
        reference = result["native"]["answer_artifact"]
        answer = self.request("GET", self.path + "/results/" + quote(result["result_id"], safe="")
                              + "/native-answer?revision=" + str(result["revision"]), max_bytes=1100000)
        if (answer.get("result_id") != result["result_id"] or answer.get("revision") != result["revision"]
                or answer.get("run_id") != result["run_id"] or answer.get("artifact") != reference
                or not isinstance(answer.get("text"), str) or not answer["text"].strip()
                or hashlib.sha256(answer["text"].encode("utf-8")).hexdigest() != reference["sha256"]):
            raise ValueError("original native answer content differs from versioned Product evidence")
        return answer

    def client_environment(self):
        selected = self.manifest
        excluded = {selected.get(key) for key in ("api_token_env", "mcp_token_env", "readback_token_env")}
        environment = {key: value for key, value in os.environ.items()
                       if key not in excluded and key in {
                           "HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "PLAYWRIGHT_BROWSERS_PATH"}}
        environment["ADP_HOME"] = str(Path(selected["cli_home"]) / ".adp")
        environment["BG_CONFIG_DIR"] = selected.get("cli_config_dir", str(Path(selected["cli_home"]) / ".bedrock-gateway"))
        environment["ADP_LEGACY_CONFIG_DIR"] = str(Path(selected["cli_home"]) / ".bedrock-gateway")
        return environment

    def check_browser_dependencies(self):
        environment = self.client_environment()
        if not Path(environment.get("PLAYWRIGHT_BROWSERS_PATH", "")).is_absolute():
            raise ValueError("FinOps browser dependencies require an absolute shared cache path")
        response = subprocess.run(["node", str(Path(__file__).with_name("browser.mjs")), "--check-dependencies"],
                                  capture_output=True, cwd=ROOT / "modules/gateway/frontend", env=environment,
                                  timeout=min(self.remaining(), 45))
        if (response.returncode or len(response.stdout) > 1024
                or json.loads(response.stdout) != {"browser": "chromium", "ready": True}):
            raise ValueError("FinOps browser dependencies are unavailable")

    def cli_result(self, result):
        selected = self.manifest
        command = [selected["cli_executable"], "--deployment", selected["cli_deployment"],
                   "service", "result", "show", "--service", "finops", "--product",
                   selected["scope"]["product_id"], "--project", selected["scope"]["project_id"],
                   "--result-id", result["result_id"], "--json"]
        environment = self.client_environment()
        response = subprocess.run(command, capture_output=True, env=environment,
                                  timeout=min(self.remaining(), 20))
        if response.returncode or len(response.stdout) > 65536:
            raise ValueError("installed CLI result is unavailable")
        payload = json.loads(response.stdout)
        if payload.get("status") != "ok":
            raise ValueError("installed CLI result was not acknowledged")
        observed = self._same_result(payload.get("detail"), result)
        self.client_observations["cli"] = observed
        return observed

    def mcp_result(self, result):
        scope = self.manifest["scope"]
        observed = self.request("POST", "/api/v1/l5/mcp/tools/call", token="mcp_token_env",
                                body={"tool": "l5___get_result", "arguments": {
                                    "product_id": scope["product_id"], "project_id": scope["project_id"],
                                    "service_id": "finops", "result_id": result["result_id"]}}, status=200)
        data = observed.get("structuredContent") or {}
        if observed.get("isError") or data.get("http_status") != 200:
            raise ValueError("scoped MCP result unavailable")
        checked = self._same_result(data.get("data"), result)
        self.client_observations["mcp"] = checked
        return checked

    def ui_result(self, result, answer=None):
        selected = self.manifest
        answer = answer or self.native_answer(result)
        input_data = {"frontend_url": selected["frontend_url"], "scope": selected["scope"],
                      "result_id": result["result_id"], "revision": result["revision"],
                      "storage_state": selected["browser_storage_state"],
                      "native_artifact": result["native"]["answer_artifact"],
                      "billing_pages": result["oracle"]["pages"], "answer_text": answer["text"]}
        response = subprocess.run(["node", str(Path(__file__).with_name("browser.mjs"))],
                                  input=json.dumps(input_data).encode(), capture_output=True,
                                  cwd=ROOT / "modules/gateway/frontend", env=self.client_environment(),
                                  timeout=min(self.remaining(), 45))
        if response.returncode or len(response.stdout) > 65536:
            raise ValueError("first-party UI result was unavailable")
        output = json.loads(response.stdout)
        if output != {"result_id": result["result_id"], "revision": result["revision"],
                      "native_artifact": result["native"]["answer_artifact"],
                      "billing_pages": result["oracle"]["pages"], "answer_text": answer["text"]}:
            raise ValueError("first-party UI displayed a different native or billing result")
        self.client_observations["ui"] = output
        return output

    def publication(self, result):
        status = self.request("GET", self.path + "/results/" + quote(result["result_id"], safe="")
                              + "/publication-status")
        consumers = status.get("consumers")
        if (status.get("result_id") != result["result_id"] or status.get("revision") != result["revision"]
                or not isinstance(consumers, list) or len(consumers) != 2
                or {item.get("destination") for item in consumers} != {
                    "product-context", "engineering-insights"}
                or any(item.get("state") != "published" or not item.get("receipt_id")
                       or item.get("projection_verified_on_ack") is not True for item in consumers)):
            raise ValueError("original result is not published to both scoped consumers")
        return status

    def emit_observation(self, run, result, answer, clients, publication):
        path = self.directory / f"finops-{self.manifest['receipt']}-qualification.json"
        if path.exists() or path.is_symlink():
            raise ValueError("qualification observation already exists")
        if (not self.submitted or run.get("run_id") != self.owned_run_id
                or clients != self.client_observations
                or self.read_run(self.owned_run_id) != run
                or self.read_result(self.owned_run_id) != result
                or self.native_answer(result) != answer
                or self.publication(result) != publication):
            raise ValueError("qualification observations are not from the original Product run")
        verified = evaluate(self.manifest, run, result, answer, clients, publication)
        native_ref = run.get("native_job_ref") or {}
        native = result["native"]
        scope = self.manifest["scope"]
        billing = (run.get("admission") or {}).get("billing_scope") or {}
        snapshot = result["billing_scope"].get("snapshot_artifact")
        if (not isinstance(native_ref, dict)
                or any(native_ref.get(field) != scope[field] for field in scope)
                or native_ref.get("agent_id") != self.manifest["native_agent_ref"]["record_id"]
                or native_ref.get("conversation_id") != native["conversation_id"]
                or any(native_ref.get(field) != native.get(field) for field in ("turn_id", "turn_locator"))
                or native_ref.get("question_hash") != hashlib.sha256(
                    self.manifest["input"]["question"].encode()).hexdigest()
                or result.get("binding_generation") != (run.get("admission") or {}).get("binding_generation")
                or result.get("scope_digest") != billing.get("scope_digest")
                or not isinstance(snapshot, dict) or set(snapshot) != {"artifact_id", "version", "sha256"}
                or not all(isinstance(snapshot[field], str) and snapshot[field] for field in snapshot)
                or not re.fullmatch(r"[a-f0-9]{64}", snapshot["sha256"])
                or snapshot != billing.get("snapshot_artifact")
                or any(result["billing_scope"].get(field) != billing.get(field)
                       for field in ("view_ref", "account_ids"))):
            raise ValueError("native turn or independent billing scope differs from durable Product work")

        def digest(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        record = {"kind": "finops_qualification_observation", "validation": "schema_only",
                  "selected_release_sha": verified["release_sha"], "scope": verified["scope"],
                  "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                  "configuration_revision": self.manifest["configuration_revision"],
                  "binding_id": self.manifest["binding_id"], "run_id": verified["run_id"],
                  "result_id": verified["result_id"], "revision": verified["revision"],
                  "result_sha256": digest(result), "run_readback_sha256": digest(run),
                  "native": {"conversation_id": native["conversation_id"],
                             **{field: native[field] for field in ("turn_id", "turn_locator") if native.get(field)},
                             "answer_artifact": verified["native_artifact"]},
                  "billing": {"view_ref": billing["view_ref"], "account_ids": billing["account_ids"],
                              "scope_digest": billing["scope_digest"], "snapshot_artifact": snapshot,
                              "query_hash": result["oracle"]["query_hash"],
                              "collection_id": verified["billing_collection_id"],
                              "pages": verified["billing_pages"]},
                  "clients": {surface: digest(clients[surface]) for surface in ("cli", "mcp", "ui")},
                  "publication": {item["destination"]: {"receipt_id": item["receipt_id"],
                                   "projection_verified_on_ack": item["projection_verified_on_ack"]}
                                  for item in publication["consumers"]}}
        record["sha256"] = digest(record)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        directory_descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return path

    def cleanup(self, run_id):
        run = self.read_run(run_id)
        if run.get("state") in {"succeeded", "partial", "failed", "cancelled", "expired"}:
            return {"status": "retained", "run_id": run_id}
        if run.get("state") not in {"admitted", "queued"} or run.get("provider_state") != "not_started":
            return {"status": "uncertain", "run_id": run_id}
        return self.request("POST", self.path + "/runs/" + quote(run_id, safe="") + "/cancel",
                            token="api_token_env", body={"reason": "qualification owned cleanup"},
                            receipt=self.manifest["cleanup_receipt"])


def run_qualification(config, evidence_dir):
    driver = FinOpsDriver(config, evidence_dir)
    try:
        run_id = driver.submit_once()
        while True:
            current = driver.read_run(run_id)
            if current["state"] in {"succeeded", "partial"}:
                break
            if current["state"] in {"failed", "cancelled", "expired", "outcome_unknown"}:
                raise ValueError("native run did not finish with a confirmed answer")
            time.sleep(min(driver.remaining(), 2))
        result = driver.read_result(run_id)
        answer = driver.native_answer(result)
        clients = {"cli": driver.cli_result(result), "mcp": driver.mcp_result(result),
                   "ui": driver.ui_result(result, answer)}
        publication = driver.publication(result)
        if driver.cleanup(run_id) != {"status": "retained", "run_id": run_id}:
            raise ValueError("original native result cannot be retained after owned cleanup")
        driver.emit_observation(current, result, answer, clients, publication)
        driver.journal("qualification_observed", run_id)
    finally:
        driver.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="One authorized Product-scoped FinOps inquiry")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--evidence-dir", required=True)
    args = parser.parse_args()
    run_qualification(json.loads(private_path(args.manifest).read_text()), args.evidence_dir)


if __name__ == "__main__":
    main()
