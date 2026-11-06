"""Real CLI, served-browser and MCP adapters for protected setup execution."""

import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from clients import api_reader
from evidence import AttemptJournal


ROOT = Path(__file__).resolve().parents[4]
CLI = ROOT / "modules/gateway/cli/adp-service.py"


def fixture_cli_environment(case):
    """Bind a child to the fixture session, never the operator's tenant pin."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("ADP_TENANT_", "ADP_DEPLOYMENT_"))
                   and key not in {"ADP_DEPLOYMENT", "BG_CONFIG_DIR", "ADP_STATE_DIR",
                                   "ADP_RUNTIME_DIR", "ADP_LOG_DIR"}}
    environment.update(ADP_HOME=case["cli_home"], ADP_DEPLOYMENT_ID=case["deployment_id"])
    return environment


class CliOperationError(AssertionError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__("Selected CLI operation failed; inspect its private attempt and canonical receipt")


class McpOperationError(AssertionError):
    """A delivered MCP response rejected the operation, not a journal check."""

    def __init__(self, status_code, code):
        self.status_code = status_code
        self.code = code
        super().__init__(f"Selected MCP operation failed ({status_code}: {code}); inspect its original receipt")


class LiveSetupDriver:
    def __init__(self, case, page, *, existing_journal=False):
        self.case = case
        self.page = page
        self.deadline = time.monotonic() + case["deadline_seconds"]
        if self.page is not None:
            self.page.set_default_timeout(min(20000, case["deadline_seconds"] * 1000))
            self.page.set_default_navigation_timeout(min(20000, case["deadline_seconds"] * 1000))
        scope = case["scope"]
        self.path = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                     f"{quote(scope['project_id'], safe='')}/services/security")
        self.api = httpx.Client(base_url=case["api_url"], timeout=20, follow_redirects=False,
                                headers={"Authorization": "Bearer " + os.environ[case["api_token_env"]]})
        self.mcp = httpx.Client(base_url=case["api_url"], timeout=20, follow_redirects=False,
                                headers={"Authorization": "Bearer " + os.environ[case["mcp_token_env"]]})
        self.read_api = api_reader(self.api)
        self.journal = AttemptJournal(case["journal_file"])
        if self.journal.path.exists() and not existing_journal:
            raise AssertionError("setup journal already exists; reconcile previous attempts before a new run")

    def close(self):
        self.api.close()
        self.mcp.close()

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("live setup exceeded authorized deadline")
        return min(left, 20)

    def cli(self, category, verb=None, *, resource=None, input_file=None, match=None, receipt=None):
        self.remaining()
        command = [sys.executable, str(CLI), category]
        if verb:
            command.append(verb)
        command += ["--product", self.case["scope"]["product_id"], "--project",
                    self.case["scope"]["project_id"], "--service", "security", "--json"]
        if resource:
            name, value = resource
            command.extend(("--" + name.replace("_", "-"), value))
        if input_file:
            command.extend(("--input", str(input_file)))
        if match:
            command.extend(("--if-match", match))
        if receipt:
            command.extend(("--idempotency-key", receipt))
        environment = fixture_cli_environment(self.case)
        response = subprocess.run(command, env=environment, capture_output=True, text=True,
                                  timeout=self.remaining(), check=False)
        if response.returncode != 0:
            try:
                status = json.loads(response.stdout).get("error", {}).get("http_status")
            except (ValueError, AttributeError):
                status = None
            raise CliOperationError(status)
        envelope = json.loads(response.stdout)
        if envelope["status"] != "ok":
            raise AssertionError("selected CLI operation did not succeed")
        return envelope

    def cli_configuration(self):
        envelope = self.cli("config", "show")
        return {**envelope["detail"], "etag": envelope["etag"]}

    def denied_cli_configuration(self, foreign):
        command = [sys.executable, str(CLI), "config", "show", "--product", foreign["scope"]["product_id"],
                   "--project", foreign["scope"]["project_id"], "--service", getattr(self, "service_id", "security"), "--json"]
        response = subprocess.run(command, env=fixture_cli_environment(self.case), capture_output=True,
                                  text=True, timeout=self.remaining(), check=False)
        self.remaining()
        return response.returncode, json.loads(response.stdout)

    def denied_mcp_configuration(self, foreign):
        response = self.mcp.post("/v1/l5/mcp/tools/call", json={"tool": "l5___get_configuration", "arguments": {
            "product_id": foreign["scope"]["product_id"], "project_id": foreign["scope"]["project_id"],
            "service_id": getattr(self, "service_id", "security"),
        }}, timeout=self.remaining())
        self.remaining()
        return response.status_code, response.json()

    def cli_binding(self, binding_id):
        return self.cli("binding", "show", resource=("binding_id", binding_id))["detail"]

    def verify_provider_authority(self, provider):
        result = self.read_api(self.path + "/release-evidence/provider/"
                               + quote(self.case["selected"]["binding_id"], safe=""))
        expected = {key: provider[key] for key in ("account_id", "role_arn", "region")}
        if (result.get("provider_identity") != expected
                or result.get("configuration_id") != self.case["selected"]["configuration_id"]
                or result.get("binding_id") != self.case["selected"]["binding_id"]):
            raise AssertionError("Product-authorized provider identity differs from the selected fixture")

    def cli_update(self, settings_path, etag, receipt):
        self.record(receipt, "cli_update", "prepared")
        updated = self.cli("config", "update", input_file=settings_path, match=etag, receipt=receipt)["detail"]
        self.record(receipt, "cli_update", "acknowledged", updated["configuration_id"], receipt)
        return updated

    def ui_edit(self, region, reason):
        if self.page is None:
            raise AssertionError("browser work was not selected")
        self.remaining()
        self.record(self.case["ui_receipt"], "ui_edit", "prepared")
        destination = (self.case["ui_url"].rstrip("/") + "/products/"
                       + quote(self.case["scope"]["product_id"], safe="") + "/projects/"
                       + quote(self.case["scope"]["project_id"], safe="") + "/services/security")
        self.page.goto(destination)
        if self.page.url != destination:
            raise AssertionError("served UI navigated away from selected target")
        setup = self.page.get_by_role("region", name="Security configuration")
        region_control = setup.get_by_label("AWS region")
        if region_control.evaluate("element => element.tagName") == "SELECT":
            region_control.select_option(region)
        else:
            region_control.fill(region)
        setup.get_by_label("Reason for explicit Security choices").fill(reason)
        with self.page.expect_response(lambda response: response.request.method == "PUT"
                                       and response.url.split("?", 1)[0].endswith(self.path + "/configuration")) as saved:
            setup.get_by_role("button", name="Save Security configuration").click()
        response = saved.value
        if response.status != 200:
            raise AssertionError("served UI conditional edit failed")
        edited = response.json()
        setup.get_by_text(re.compile(rf"saved at revision {edited['revision']}\b")).wait_for(
            state="visible", timeout=self.remaining() * 1000)
        actual_key = response.request.header_value("idempotency-key")
        if not actual_key:
            raise AssertionError("served UI did not send an idempotency key")
        self.record(self.case["ui_receipt"], "ui_edit", "acknowledged", edited["configuration_id"], actual_key)
        return edited

    def mcp_call(self, name, arguments, status=200):
        self.remaining()
        response = self.mcp.post("/v1/l5/mcp/tools/call", json={"tool": name, "arguments": arguments},
                                 timeout=self.remaining())
        response.raise_for_status()
        payload = response.json()
        detail = payload["structuredContent"]
        if payload.get("isError") or detail["http_status"] != status:
            code = detail.get("error", {}).get("code")
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
                code = "request_failed"
            raise McpOperationError(detail["http_status"], code)
        return detail["data"]

    def mcp_submit(self, run_input, receipt):
        self.record(receipt, "mcp_submit", "prepared")
        submitted = self.mcp_call("l5___submit_run", {**self._scope(), "input": run_input,
                                                     "idempotency_key": receipt}, status=202)
        self.record(receipt, "mcp_submit", "acknowledged", submitted["run_id"], receipt)
        return submitted

    def record(self, receipt, operation, status, effect_id=None, idempotency_key=None):
        if status == "prepared":
            previous = self.journal.read() if self.journal.path.exists() else []
            if (any(entry["operation"] == operation or entry["attempt_id"] == receipt for entry in previous)
                    or sum(entry["status"] == "prepared" for entry in previous) >= self.case["max_attempts"]):
                raise AssertionError("setup attempt already started or exceeds the authorized bound")
        run_id = effect_id if operation == "mcp_submit" and status == "acknowledged" else None
        self.journal.append({"attempt_id": receipt, "run_id": run_id,
                             "operation": operation, "status": status, "work_id": receipt,
                             "observed_at": datetime.now(UTC).isoformat(), "effect_id": effect_id,
                             "idempotency_key": idempotency_key})

    def attempts(self, selected):
        entries = self.journal.read()
        operations = (("cli_update", "cli", self.case["cli_receipt"]),
                      ("ui_edit", "ui", self.case["ui_receipt"]),
                      ("mcp_submit", "mcp", self.case["mcp_receipt"]))
        if len(entries) != len(operations) * 2:
            raise AssertionError("setup journal is missing an attempt or contains an extra effect")
        for operation, _, receipt in operations:
            prepared = [entry for entry in entries if entry["operation"] == operation and entry["status"] == "prepared"]
            acknowledged = [entry for entry in entries if entry["operation"] == operation and entry["status"] == "acknowledged"]
            if (len(prepared) != 1 or len(acknowledged) != 1
                    or prepared[0]["attempt_id"] != receipt or acknowledged[0]["attempt_id"] != receipt
                    or acknowledged[0]["effect_id"] != selected["effects"][operation]
                    or acknowledged[0]["idempotency_key"] is None):
                raise AssertionError("setup journal does not reconcile the selected effect")
        return [{"operation": operation, "surface": surface,
                 "effect_id": selected["effects"][operation]} for operation, surface, _ in operations]

    def _scope(self):
        return {"product_id": self.case["scope"]["product_id"],
                "project_id": self.case["scope"]["project_id"], "service_id": "security"}

    def cli_observations(self, binding_id, run_id):
        return {"configuration": self.cli_configuration(), "binding": self.cli_binding(binding_id),
                "run": self.cli("status", resource=("run_id", run_id))["detail"]}

    def mcp_observations(self, binding_id, run_id):
        scope = self._scope()
        return {"configuration": self.mcp_call("l5___get_configuration", scope),
                "binding": self.mcp_call("l5___get_binding", {**scope, "binding_id": binding_id}),
                "run": self.mcp_call("l5___get_run", {**scope, "assessment_ref": run_id})}

    def ui_observations(self, binding_id, run_id):
        if self.page is None:
            raise AssertionError("browser work was not selected")
        def read(path):
            result = self.page.evaluate("""async path => {
                const base = window.__ADP_CONFIG__?.VITE_API_URL || '/api';
                const token = sessionStorage.getItem('cognito_access_token');
                if (!token) throw Error('served UI has no authorized access token');
                const response = await fetch(base.replace(/\\/$/, '') + path, {
                    headers: { Authorization: 'Bearer ' + token, 'Cache-Control': 'no-cache' },
                    redirect: 'error' });
                if (!response.ok) throw Error('served UI readback failed');
                return await response.json(); }""", path)
            return result
        return {"configuration": read(self.path + "/configuration"),
                "binding": read(self.path + "/bindings/" + quote(binding_id, safe="")),
                "run": read(self.path + "/runs/" + quote(run_id, safe=""))}
