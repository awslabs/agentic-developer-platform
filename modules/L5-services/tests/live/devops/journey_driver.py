"""Exercise installed first-party clients against one owner-authorized Product."""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

from qualification import ROOT
from native_usage import verify_native_usage
from installed_results import read_investigation_result

ENTRIES = ("product", "cli", "mcp", "ui")
HEADLESS_ENTRIES = ("product", "cli", "mcp")


def selected_entries(entries):
    if entries not in (ENTRIES, HEADLESS_ENTRIES):
        raise ValueError("journeys must select full acceptance or Product/CLI/MCP progress")
    return entries


TASK_TYPES = {"investigation": "INVESTIGATION", "release_readiness": "RELEASE_READINESS_REVIEW",
              "release_verification": "RELEASE_TESTING"}


def private_path(value, *, directory=False, executable=False):
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or path.is_relative_to(ROOT)
            or path.resolve().is_relative_to(ROOT)):
        raise ValueError("first-party client needs an owner-controlled private path")
    mode = path.stat().st_mode
    if ((not stat.S_ISDIR(mode) if directory else not stat.S_ISREG(mode))
            or mode & (0o022 if executable else 0o077)):
        raise ValueError("first-party client needs a protected existing path")
    return path


def validate_driver(manifest, *, entries=ENTRIES):
    selected_entries(entries)
    driver = manifest.get("journey_driver")
    if not isinstance(driver, dict):
        raise ValueError("first-party journeys need private client configuration")
    scope = driver.get("scope", {})
    if (not isinstance(scope, dict) or set(scope) != {"product_id", "project_id", "binding_id"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[\w-]{1,128}", value)
                   for value in scope.values())):
        raise ValueError("first-party journeys need one exact Product/project/binding")
    base = urlsplit(driver.get("gateway_url", ""))
    if (base.scheme != "https" or not base.hostname or base.username or base.password
            or base.query or base.fragment or base.path not in ("", "/")
            or ("ui" in entries and not re.fullmatch(r"[\w.-]+", driver.get("provider_hostname", "")))):
        raise ValueError("first-party clients need a private HTTPS gateway and native host")
    inputs = driver.get("inputs")
    receipts = driver.get("receipts")
    if (not isinstance(inputs, dict) or set(inputs) not in (set(entries), set(ENTRIES))
            or not isinstance(receipts, dict) or set(receipts) != set(entries) - {"ui"}
            or any(not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt)
                   for receipt in receipts.values()) or len(set(receipts.values())) != 3):
        raise ValueError("each first-party operation needs an input and unique reconciliation receipt")
    for entry, body in inputs.items():
        if (not isinstance(body, dict) or body.get("service_id") != "devops"
                or body.get("binding_id") != scope["binding_id"]
                or type(body.get("configuration_revision")) is not int or body["configuration_revision"] < 1
                or body.get("capability") not in {"investigation", "release_readiness", "release_verification"}
                or not isinstance(body.get("devops_input"), dict)
                or body["devops_input"].get("kind") != body["capability"]):
            raise ValueError("first-party operation input must use one selected DevOps binding and capability")
    for name in ("api_token_env", "mcp_token_env"):
        key = driver.get(name, "")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]+", key) or not os.environ.get(key):
            raise ValueError("first-party operations need distinct private credentials")
    selected = {os.environ[channel["token_env"]] for channel in manifest.get("readback_channels", {}).values()}
    if "installed_journeys" in manifest:
        token = os.environ.get(manifest["installed_journeys"].get("token_env", ""))
        if not token:
            raise ValueError("installed journey observations need independent read credentials")
        selected.add(token)
    selected.update((os.environ[driver["api_token_env"]], os.environ[driver["mcp_token_env"]]))
    if len(selected) != len(manifest.get("readback_channels", {})) + 2 + int("installed_journeys" in manifest):
        raise ValueError("first-party action and independent readback credentials must differ")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,62}", driver.get("cli_deployment", "")):
        raise ValueError("installed CLI needs an explicitly selected deployment")
    cli = private_path(driver.get("cli_executable"), executable=True)
    if not os.access(cli, os.X_OK) or hashlib.sha256(cli.read_bytes()).hexdigest() != driver.get("cli_sha256"):
        raise ValueError("installed CLI differs from the reviewed private pin")
    private_path(driver.get("cli_home"), directory=True)
    if "ui" in entries:
        from manual_ui import validate_manual
        if validate_manual(manifest) is None:
            private_path(driver.get("browser_storage_state"))
    if "ui" in entries and manifest["ingress_policy"]["launch_path"] != (
            f"/api/v1/products/{scope['product_id']}/projects/{scope['project_id']}"
            f"/services/devops/bindings/{scope['binding_id']}/browser-launch"):
        raise ValueError("browser launch must match independently reviewed ingress")
    selected = driver.get("provider_readback")
    if (not isinstance(selected, dict) or set(selected) != {
            "connection_label", "account_id", "role_arn", "region", "source_connection_id"}
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", selected["connection_label"])
            or not re.fullmatch(r"[0-9]{12}", selected["account_id"])
            or not re.fullmatch(rf"arn:aws:iam::{selected['account_id']}:role/[\w+=,.@/-]+", selected["role_arn"])
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", selected["region"])
            or not re.fullmatch(r"[\w-]{1,128}", selected["source_connection_id"])):
        raise ValueError("native provider readback requires one selected scoped AWS connection")
    return driver


def read_provider_task(selected, space_id, task_id, *, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ValueError("native provider readback exceeded authorized deadline")
    command = ["adp-cred", "assume", "--service", "aws", "--label", selected["connection_label"],
               "--exec", sys.executable, str(Path(__file__).with_name("provider_task.py")),
               selected["account_id"], selected["role_arn"], selected["region"], space_id, task_id]
    try:
        result = subprocess.run(command, capture_output=True, timeout=min(remaining, 15), check=False)
        if result.returncode or len(result.stdout) > 4096:
            raise ValueError("selected native provider readback unavailable")
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        raise ValueError("selected native provider readback unavailable") from None


class FirstPartyDriver:
    def __init__(self, manifest, directory, *, deadline, max_requests):
        import httpx
        self.manifest = manifest
        self.driver = manifest["journey_driver"]
        self.directory = directory
        self.deadline = deadline
        self.max_requests = max_requests
        self.calls = 0
        self.result_readbacks = {}
        self.native_readbacks = {}
        self.client = httpx.Client(follow_redirects=False, timeout=15)
        scope = self.driver["scope"]
        self.path = (f"/api/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                     f"{quote(scope['project_id'], safe='')}/services/devops")

    def close(self):
        self.client.close()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("first-party journey exceeded authorized deadline")
        return remaining

    def journal(self, entry, status, receipt, run_id=None, *, operation_id=None):
        path = self.directory / f"devops-{self.manifest['authorization']['run_id']}-journey-actions.jsonl"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as sink:
            sink.write(json.dumps({"entry_point": entry, "status": status, "receipt": receipt,
                                   "run_id": run_id, **({"operation_id": operation_id} if operation_id else {})}, sort_keys=True) + "\n")
            sink.flush()
            os.fsync(sink.fileno())

    def request(self, method, path, *, token_env, body=None, receipt=None, raw=False, max_bytes=65536, if_match=None):
        self.calls += 1
        if self.calls > self.max_requests:
            raise ValueError("first-party request bound exceeded")
        with self.client.stream(method, self.driver["gateway_url"].rstrip("/") + path,
                                       json=body, headers={
                                           "Authorization": "Bearer " + os.environ[self.driver[token_env]],
                                           **({"If-Match": if_match} if if_match else {}),
                                           **({"Idempotency-Key": receipt} if receipt else {})},
                                       timeout=min(self.remaining(), 15)) as response:
            if response.status_code not in ({200, 202} if method == "POST" else {200}):
                raise ValueError("first-party API denied or returned an oversized response")
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ValueError("first-party API denied or returned an oversized response")
        if raw:
            return bytes(content)
        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("first-party API did not return a run")
        return result

    def run_product(self):
        return self.request("POST", self.path + "/runs", token_env="api_token_env",
                            body=self.driver["inputs"]["product"], receipt=self.driver["receipts"]["product"])

    def cli_environment(self):
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"ADP_HOME", "ADP_DEPLOYMENT", "ADP_GATEWAY_URL", "BG_CONFIG_DIR"}
                       and not key.startswith("ADP_DEPLOYMENT_")}
        environment["HOME"] = self.driver["cli_home"]
        environment["ADP_HOME"] = str(Path(self.driver["cli_home"]) / ".adp")
        return environment

    def cli_command(self, *args):
        return [self.driver["cli_executable"], "--deployment", self.driver["cli_deployment"], *args]

    def check_cli_target(self):
        executable = Path(self.driver["cli_executable"])
        private_path(executable.parent, directory=True, executable=True)
        resolver = private_path(executable.with_name("adp_deployments.py"), executable=True)
        result = subprocess.run([sys.executable, str(resolver), "resolve", "--deployment",
                                 self.driver["cli_deployment"], "--format", "json"],
                                capture_output=True, timeout=min(self.remaining(), 20), env=self.cli_environment())
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError("installed CLI deployment cannot be independently resolved")
        metadata = json.loads(result.stdout)
        if (not isinstance(metadata, dict) or metadata.get("deployment") != self.driver["cli_deployment"]
                or metadata.get("gateway_url", "").rstrip("/") != self.driver["gateway_url"].rstrip("/") + "/api"):
            raise ValueError("installed CLI is connected to a different gateway")

    def run_cli(self):
        input_path = self.directory / f"devops-{self.manifest['authorization']['run_id']}-cli-input.json"
        descriptor = os.open(input_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as destination:
            json.dump(self.driver["inputs"]["cli"], destination)
            destination.flush()
            os.fsync(destination.fileno())
        scope = self.driver["scope"]
        command = self.cli_command("service", "run", "--service", "devops",
                   "--product", scope["product_id"], "--project", scope["project_id"],
                   "--input", str(input_path), "--idempotency-key", self.driver["receipts"]["cli"], "--json")
        result = subprocess.run(command, capture_output=True, timeout=min(self.remaining(), 30),
                                env=self.cli_environment())
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError("installed CLI submission failed or is uncertain; reconcile receipt privately")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("status") not in {"ok", "accepted"}:
            raise ValueError("installed CLI did not acknowledge native submission")
        return payload.get("detail")

    def run_mcp(self):
        response = self.request("POST", "/api/v1/l5/mcp/tools/call", token_env="mcp_token_env",
                                body={"tool": "l5___submit_run", "arguments": {
                                    "product_id": self.driver["scope"]["product_id"],
                                    "project_id": self.driver["scope"]["project_id"], "service_id": "devops",
                                    "input": self.driver["inputs"]["mcp"],
                                    "idempotency_key": self.driver["receipts"]["mcp"]}})
        if response.get("isError") or response.get("structuredContent", {}).get("http_status") != 202:
            raise ValueError("MCP tool did not acknowledge native submission")
        return response["structuredContent"].get("data")

    def run_ui(self):
        if self.manifest.get("manual_ui") is not None:
            from manual_ui import ManualDevops
            configuration = self.request("GET", self.path + "/configuration", token_env="api_token_env")
            return ManualDevops(self.manifest, self.directory, deadline=self.deadline).submission(configuration)
        if os.environ.get("L5_BROWSER_AUTOMATION", "disabled") != "allowed":
            raise ValueError("DevOps UI requires authenticated human evidence while browser automation is disabled")
        command = ["node", str(Path(__file__).with_name("browser.mjs"))]
        payload = {"url": self.driver["gateway_url"], "scope": self.driver["scope"],
                   "storage_state": self.driver["browser_storage_state"],
                   "input": self.driver["inputs"]["ui"], "provider_hostname": self.driver["provider_hostname"],
                   "journal": str(self.directory / f"devops-{self.manifest['authorization']['run_id']}-journey-actions.jsonl")}
        result = subprocess.run(command, input=json.dumps(payload).encode(), capture_output=True,
                                timeout=min(self.remaining(), 45), cwd=ROOT / "modules/gateway/frontend")
        if result.returncode or len(result.stdout) > 65536:
            raise ValueError("first-party browser run or native handoff failed; reconcile private journal")
        return json.loads(result.stdout)

    def read_native(self, run_id):
        for attempt in range(self.max_requests - self.calls):
            observed = self.request("GET", self.path + "/runs/" + quote(run_id, safe=""), token_env="api_token_env")
            scope = observed.get("scope") or {}
            if (observed.get("run_id") != run_id or observed.get("origin") != "adp_submitted"
                    or not isinstance(scope, dict)
                    or scope.get("product_id") != self.driver["scope"]["product_id"]
                    or scope.get("project_id") != self.driver["scope"]["project_id"]
                    or observed.get("input") not in self.driver["inputs"].values()):
                raise ValueError("first-party native run belongs to a different scope")
            if observed.get("state") == "succeeded":
                native = observed.get("native_job_ref", {})
                native_id = native.get("record_id") if isinstance(native, dict) else None
                selected = self.driver["provider_readback"]
                admission = observed.get("admission") or {}
                space = admission.get("native_space_ref") or {}
                connection = admission.get("connection_ref") or {}
                if (observed.get("provider_state") != "completed" or not isinstance(native_id, str)
                        or not native_id or native.get("record_type") != "native_job"
                        or native.get("project_id") != self.driver["scope"]["project_id"]
                        or native.get("source_connection_id") != selected["source_connection_id"]
                        or not isinstance(native.get("record_revision"), str) or not native["record_revision"]
                        or not isinstance(admission, dict) or not isinstance(space, dict)
                        or not isinstance(connection, dict)
                        or not isinstance(admission.get("provider_identity"), dict)
                        or any(admission["provider_identity"].get(key) != selected[key] for key in (
                            "account_id", "role_arn", "region"))
                        or connection.get("source_connection_id") != selected["source_connection_id"]
                        or space.get("source_connection_id") != selected["source_connection_id"]
                        or space.get("project_id") != self.driver["scope"]["project_id"]
                        or space.get("record_type") != "agent_space" or not space.get("record_id")):
                    raise ValueError("first-party run did not produce a native job")
                task = self.read_task(space["record_id"], native["record_revision"])
                if (not isinstance(task, dict) or task.get("agentSpaceId") != space["record_id"]
                        or task.get("taskId") != native["record_revision"]
                        or task.get("executionId") != native_id or task.get("status") != "COMPLETED"
                        or task.get("taskType") != TASK_TYPES[observed["input"]["capability"]]):
                    raise ValueError("native provider task differs from scoped Gateway run")
                if observed["input"]["capability"] == "investigation":
                    self.result_readbacks[run_id] = read_investigation_result(self, observed, task)
                self.native_readbacks[run_id] = {"run": observed, "task": task}
                return native_id
            if observed.get("state") in {"failed", "blocked", "partial", "cancelled", "expired"}:
                raise ValueError("first-party native run did not complete")
            time.sleep(min(self.remaining(), 2))
        raise ValueError("first-party native polling exceeded request bound")

    def read_task(self, space_id, task_id):
        return read_provider_task(self.driver["provider_readback"], space_id, task_id, deadline=self.deadline)


def drive_journeys(manifest, directory, *, deadline, driver_factory=FirstPartyDriver):
    config = validate_driver(manifest)
    budget = sum(manifest["limits"][config["inputs"][entry]["capability"]]["max_requests"] for entry in ENTRIES)
    journal = directory / f"devops-{manifest['authorization']['run_id']}-journey-actions.jsonl"
    descriptor = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)
    target = driver_factory(manifest, directory, deadline=deadline, max_requests=budget)
    results = {}
    try:
        target.check_cli_target()
        for entry in ENTRIES:
            receipt = config["receipts"].get(entry)
            target.journal(entry, "prepared", receipt)
            try:
                response = getattr(target, f"run_{entry}")()
                if entry == "ui":
                    if not isinstance(response, dict) or response.get("native_handoff") is not True:
                        raise ValueError("first-party browser did not validate the native UI handoff")
                    receipt = response.get("receipt")
                    if not isinstance(receipt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", receipt):
                        raise ValueError("first-party UI lacks a reconciliable run receipt")
                run_id = response.get("run_id") if isinstance(response, dict) else None
                if not isinstance(run_id, str) or not re.fullmatch(r"[\w-]{1,128}", run_id) or run_id in {value["run_id"] for value in results.values()}:
                    raise ValueError("first-party submission lacks a unique native run")
                target.journal(entry, "acknowledged", receipt, run_id)
                native_id = target.read_native(run_id)
                if not isinstance(native_id, str) or not native_id:
                    raise ValueError("first-party native result was not independently read back")
                results[entry] = {"run_id": run_id, "native_id": native_id}
                readback = getattr(target, "result_readbacks", {}).get(run_id)
                if readback is not None:
                    results[entry]["result_readback"] = readback
                target.journal(entry, "verified", receipt, run_id)
            except Exception:
                target.journal(entry, "uncertain", receipt)
                raise
        return results
    finally:
        target.close()


def verify_driven_journeys(manifest, observed, driven, *, entries=ENTRIES):
    selected_entries(entries)
    if (not isinstance(observed, list) or not isinstance(driven, dict)
            or {item.get("entry_point") for item in observed if isinstance(item, dict)} != set(entries)
            or set(driven) != set(entries)):
        raise ValueError("selected first-party journeys need independent native readbacks")
    spend = {}
    for item in observed:
        entry = item["entry_point"]
        if (item.get("status") != "completed"
                or item.get("run_id") != driven[entry]["run_id"]
                or item.get("native_id") != driven[entry]["native_id"]
                or item.get("scope") != manifest["journey_driver"]["scope"]):
            raise ValueError("first-party journey observation differs from native operation")
        usage = item.get("usage", {})
        capability = manifest["journey_driver"]["inputs"][entry]["capability"]
        limit = manifest["limits"][capability]
        coverage, amount = verify_native_usage(usage, limit, minimum_requests=1)
        previous = spend.setdefault(capability, {"measured": [], "estimated": [], "unavailable": [], "requests": 0})
        previous[coverage].append(amount)
        previous["requests"] += usage["requests"]
    summary = {}
    for capability, observed_usage in spend.items():
        measured = sum(observed_usage["measured"], Decimal(0))
        estimated = sum(observed_usage["estimated"], Decimal(0))
        if (measured + estimated > Decimal(str(manifest["limits"][capability]["max_spend_usd"]))
                or observed_usage["requests"] > manifest["limits"][capability]["max_requests"]):
            raise ValueError("first-party native operations exceeded shared capability budget")
        complete = not observed_usage["estimated"] and not observed_usage["unavailable"]
        summary[capability] = {
            "measured_spend_usd": str(measured) if observed_usage["measured"] else None,
            "estimated_spend_usd": str(estimated) if observed_usage["estimated"] else None,
            "total_spend_usd": str(measured) if complete else None,
            "unavailable_task_count": len(observed_usage["unavailable"]),
            "requests": observed_usage["requests"],
        }
    return summary
