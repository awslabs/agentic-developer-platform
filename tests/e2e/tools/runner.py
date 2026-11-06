"""Protected tools evaluation via the installed stdio MCP client."""

import argparse
import base64
import fnmatch
import hashlib
import importlib
import json
import math
import os
import re
import select
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen
from uuid import UUID, uuid4
from xml.etree import ElementTree

from modules.tools.scripts.reconcile_release import reconcile
from modules.tools.scripts.release_journal import decision as journal_decision, load as load_journal, prerequisites
from modules.tools.scripts.release_scope import operator_sequence
from modules.tools.scripts.release_window import verify_manifest, verify_window
from tests.e2e.tools.client_matrix import OPERATIONS, check_discovery, cli_call, import_session, native_call, prepare_owned, resolve_arguments, validate_fixture, verify_readback
from tests.e2e.tools.installed_clients import installed_clients, configure_model_transport, resume_installed_clients
from tests.e2e.tools.process_bounds import ProcessLimitError, run_bounded
from tests.e2e.tools.pricing_envelope import reserve_all_in
from tests.e2e.tools.access_integration import access_ac01, access_ac02, access_ac03
from tests.e2e.tools.browser_integration import browser_ac01, browser_ac02, browser_ac03, browser_ac04
from tests.e2e.tools.access_scenarios import ACCESS_LIVE_FIXTURE, access_verdict, run_access_fixture
from tests.e2e.tools.cleanup_integration import cleanup_ac01, cleanup_ac02, cleanup_ac03, cleanup_ac04
from tests.e2e.tools.knowledge_integration import KNOWLEDGE_OPERATIONS, knowledge_ac01, knowledge_ac02, knowledge_ac03, knowledge_ac04
from tests.e2e.tools.credential_integration import broker_ac01, broker_ac02, broker_ac03, broker_ac04
from tests.e2e.tools.governance_integration import FAMILIES as GOVERNANCE_FAMILIES
from tests.e2e.tools.governance_integration import governance_ac01, governance_ac02, governance_ac03, governance_ac04_link
from tests.e2e.tools.oauth_integration import oauth_ac01, oauth_ac02, oauth_ac03, oauth_ac04
from tests.e2e.tools.github_integration import github_ac01, github_ac02, github_ac03, github_ac04
from tests.e2e.tools.jira_integration import jira_ac01, jira_ac02, jira_ac03, jira_ac04
from tests.e2e.tools.atlassian_integration import live_connect, run_live_connect
from tests.e2e.tools.atlassian_client_matrix import CLIENTS as ATLASSIAN_CLIENTS
from tests.e2e.tools.atlassian_client_matrix import observe_cell as observe_atlassian_cell
from tests.e2e.tools.atlassian_client_matrix import load_isolated_fixtures, validate_isolated_fixtures
from tests.e2e.tools.atlassian_client_matrix import OPERATION_ORDER as ATLASSIAN_OPERATION_ORDER
from tests.e2e.tools.slack_integration import slack_ac01, slack_ac02, slack_ac03, slack_ac04
from tests.e2e.tools.release_observer import from_environment as release_observer_from_environment, private_json
from tests.e2e.tools.scenarios import (
    BASE_STORIES,
    CLIENTS,
    LIFECYCLE_FIXTURES,
    FixtureUnavailable,
    fixture_for_cell,
    fixture_summary,
    lifecycle_verdict,
    matrix_cells,
    run_fixture,
)

REVISION = re.compile(r"[0-9a-f]{40}\Z")
IMAGE = re.compile(r"[a-z0-9./-]+@sha256:[0-9a-f]{64}\Z")
REPAIR_SCENARIO = re.compile(r"SCN-([1-9][0-9]*)-(0[1-6])\Z")


@dataclass(frozen=True)
class Scenario:
    identifier: str
    stories: tuple[str, ...]
    action: str


SCENARIOS = tuple(
    Scenario("SCN-TLS-" + number, stories, "list_tools" if number == "0017" else "unavailable")
    for number, stories in BASE_STORIES.items()
) + tuple(Scenario(fixture.identifier, fixture.stories, "lifecycle") for fixture in LIFECYCLE_FIXTURES) + (
    Scenario("SCN-8064-01", ("AC-01",), "contract_ac01"),
    Scenario("SCN-8064-02", ("AC-02",), "contract_ac02"),
    Scenario("SCN-8064-03", ("AC-03",), "contract_ac03"),
    Scenario("SCN-8064-04", ("AC-04",), "contract_ac04"),
    Scenario("SCN-8067-01", ("AC-01",), "client_install"),
    Scenario("SCN-8067-02", ("AC-02",), "client_matrix"),
    Scenario("SCN-8067-03", ("AC-03",), "client_faults"),
    Scenario("SCN-8067-04", ("AC-04",), "client_evidence"),
    Scenario("SCN-8065-01", ("AC-01",), "access_ac01"),
    Scenario("SCN-8065-02", ("AC-02",), "access_ac02"),
    Scenario("SCN-8065-03", ("AC-03",), "access_ac03"),
    Scenario("SCN-8065-04", ("AC-04",), "access_live"),
    Scenario("SCN-8066-01", ("AC-01",), "cleanup_ac01"),
    Scenario("SCN-8066-02", ("AC-02",), "cleanup_ac02"),
    Scenario("SCN-8066-03", ("AC-03",), "cleanup_ac03"),
    Scenario("SCN-8066-04", ("AC-04",), "cleanup_ac04"),
    Scenario("SCN-8068-01", ("AC-01",), "release_ac01"),
    Scenario("SCN-8068-02", ("AC-02",), "release_ac02"),
    Scenario("SCN-8068-03", ("AC-03",), "release_ac03"),
    Scenario("SCN-8068-04", ("AC-04",), "release_ac04"),
    Scenario("SCN-8070-01", ("AC-01",), "interpreter_ac01"),
    Scenario("SCN-8070-02", ("AC-02",), "interpreter_ac02"),
    Scenario("SCN-8070-03", ("AC-03",), "interpreter_ac03"),
    Scenario("SCN-8070-04", ("AC-04",), "interpreter_ac04"),
    Scenario("SCN-8069-01", ("AC-01",), "search_ac01"),
    Scenario("SCN-8069-02", ("AC-02",), "search_ac02"),
    Scenario("SCN-8069-03", ("AC-03",), "search_ac03"),
    Scenario("SCN-8069-04", ("AC-04",), "search_ac04"),
    Scenario("SCN-8071-01", ("AC-01",), "browser_ac01"),
    Scenario("SCN-8071-02", ("AC-02",), "browser_ac02"),
    Scenario("SCN-8071-03", ("AC-03",), "browser_ac03"),
    Scenario("SCN-8071-04", ("AC-04",), "browser_ac04"),
    Scenario("SCN-8072-01", ("AC-01",), "knowledge_ac01"),
    Scenario("SCN-8072-02", ("AC-02",), "knowledge_ac02"),
    Scenario("SCN-8072-03", ("AC-03",), "knowledge_ac03"),
    Scenario("SCN-8072-04", ("AC-04",), "knowledge_ac04"),
    Scenario("SCN-8073-01", ("AC-01",), "broker_ac01"),
    Scenario("SCN-8073-02", ("AC-02",), "broker_ac02"),
    Scenario("SCN-8073-03", ("AC-03",), "broker_ac03"),
    Scenario("SCN-8073-04", ("AC-04",), "broker_ac04"),
    Scenario("SCN-8078-01", ("AC-01",), "governance_ac01"),
    Scenario("SCN-8078-02", ("AC-02",), "governance_ac02"),
    Scenario("SCN-8078-03", ("AC-03",), "governance_ac03"),
    Scenario("SCN-8078-04", ("AC-04",), "governance_ac04"),
    Scenario("SCN-8074-01", ("AC-01",), "oauth_ac01"),
    Scenario("SCN-8074-02", ("AC-02",), "oauth_ac02"),
    Scenario("SCN-8074-03", ("AC-03",), "oauth_ac03"),
    Scenario("SCN-8074-04", ("AC-04",), "oauth_ac04"),
    Scenario("SCN-8075-01", ("AC-01",), "github_ac01"),
    Scenario("SCN-8075-02", ("AC-02",), "github_ac02"),
    Scenario("SCN-8075-03", ("AC-03",), "github_ac03"),
    Scenario("SCN-8075-04", ("AC-04",), "github_ac04"),
    Scenario("SCN-8076-01", ("AC-01",), "jira_ac01"),
    Scenario("SCN-8076-02", ("AC-02",), "jira_ac02"),
    Scenario("SCN-8076-03", ("AC-03",), "jira_ac03"),
    Scenario("SCN-8076-04", ("AC-04",), "jira_ac04"),
    Scenario("SCN-8077-01", ("AC-01",), "slack_ac01"),
    Scenario("SCN-8077-02", ("AC-02",), "slack_ac02"),
    Scenario("SCN-8077-03", ("AC-03",), "slack_ac03"),
    Scenario("SCN-8077-04", ("AC-04",), "slack_ac04"),
    Scenario("SCN-8454-01", ("AC-01",), "atlassian_setup"),
    Scenario("SCN-8454-02", ("AC-02",), "atlassian_client_matrix"),
    Scenario("SCN-8454-03", ("AC-03",), "atlassian_authority"),
    Scenario("SCN-8454-04", ("AC-04",), "atlassian_recovery"),
    Scenario("SCN-8454-05", ("AC-05",), "atlassian_regression"),
    Scenario("SCN-8454-06", ("AC-06",), "atlassian_handoff"),
)
FIXTURES = {fixture.identifier: fixture for fixture in LIFECYCLE_FIXTURES}


def validate_scenarios(scenarios):
    identifiers = set()
    for case in scenarios:
        repair = REPAIR_SCENARIO.fullmatch(case.identifier)
        if (not (re.fullmatch(r"SCN-TLS-[0-9]{4}", case.identifier) or REPAIR_SCENARIO.fullmatch(case.identifier))
                or repair and int(repair[2]) > 4 and repair[1] != "8454"
                or case.identifier in identifiers):
            raise ValueError("scenario IDs must be unique SCN-TLS-nnnn or SCN-<issue>-0n values")
        if not case.stories or len(set(case.stories)) != len(case.stories) or any(
            not re.fullmatch(r"(?:TLS-(?:S|C|D)[0-9]{2}|AC-0[1-6])" if repair and repair[1] == "8454"
                             else r"(?:TLS-(?:S|C|D)[0-9]{2}|AC-0[1-4])", story) for story in case.stories
        ):
            raise ValueError(f"{case.identifier}: at least one valid, unique story tag is required")
        identifiers.add(case.identifier)
    if not identifiers:
        raise ValueError("scenario registry is empty")


def private_access_bindings():
    module_name = os.environ.get("ADP_TOOLS_ACCESS_FIXTURE_MODULE", "")
    if not re.fullmatch(r"tests\.e2e\.tools\.private_access_[a-z0-9_]+", module_name):
        return None, None
    try:
        module = importlib.import_module(module_name)
        return module.driver_factory, module.observer
    except (ImportError, AttributeError):
        return None, None



def private_interpreter_bindings():
    module_name = os.environ.get("ADP_TOOLS_INTERPRETER_FIXTURE_MODULE", "")
    if not re.fullmatch(r"tests\.e2e\.tools\.private_interpreter_[a-z0-9_]+", module_name):
        return None, None, None
    try:
        module = importlib.import_module(module_name)
        return module.client_fixture, module.provider_observer, module.pinned_client_factory
    except (ImportError, AttributeError):
        return None, None, None


def private_slack_bindings():
    module_name = os.environ.get("ADP_TOOLS_SLACK_FIXTURE_MODULE", "")
    if not re.fullmatch(r"tests\.e2e\.tools\.private_slack_[a-z0-9_]+", module_name):
        return None, None
    try:
        module = importlib.import_module(module_name)
        return module.driver_factory, module.observer
    except (ImportError, AttributeError):
        return None, None


def private_jira_bindings():
    module_name = os.environ.get("ADP_TOOLS_JIRA_FIXTURE_MODULE", "")
    if not re.fullmatch(r"tests\.e2e\.tools\.private_jira_[a-z0-9_]+", module_name):
        return None, None
    try:
        module = importlib.import_module(module_name)
        return module.driver_factory, module.observer
    except (ImportError, AttributeError):
        return None, None


def private_atlassian_setup_bindings(gateway_url, gateway_revision=None):
    from tests.e2e.tools.atlassian_setup_live import GatewaySetupDriver, load_setup
    from tests.e2e.tools.atlassian_setup_observer import IndependentSetupObserver

    try:
        fixture, config = load_setup(os.environ["ADP_TOOLS_ATLASSIAN_SETUP_FILE"], gateway_url=gateway_url)
        return lambda: fixture, lambda _: GatewaySetupDriver(config), IndependentSetupObserver(config, gateway_revision=gateway_revision)
    except (KeyError, OSError, TypeError, ValueError):
        return None, None, None


def private_release_bindings():
    try:
        manifest = private_json(os.environ["ADP_TOOLS_RELEASE_MANIFEST_FILE"])
        observer = release_observer_from_environment()
        return manifest, observer.readback
    except (OSError, KeyError, TypeError, ValueError):
        return None, None


def select_scenarios(pattern, scenarios=SCENARIOS):
    if re.fullmatch(r"SCN-0[1-4]", pattern):
        raise ValueError("ambiguous local scenario ID; use a story-qualified selector such as SCN-8064-01 or SCN-8065-01")
    selected = [case for case in scenarios if (pattern in {"full", "l5"} and
                case.identifier.startswith("SCN-TLS-")) or
                (pattern == "repair" and REPAIR_SCENARIO.fullmatch(case.identifier)) or
                fnmatch.fnmatchcase(case.identifier, pattern)]
    if not selected:
        raise ValueError("scenario filter selected no registered scenarios")
    return selected


class MCPClient:
    def __init__(self, command, *, environ=None):
        self.command = command
        self.environ = environ
        self.process = None
        self.sequence = 0
        self.version = None
        self.pending = bytearray()

    def __enter__(self):
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, env=self.environ)
        try:
            initialized = self.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                      "clientInfo": {"name": "adp-tools-evaluation", "version": "1"}})
            self.version = initialized["serverInfo"]["version"]
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate(timeout=3)

    def request_raw(self, method, params=None):
        self.sequence += 1
        payload = {"jsonrpc": "2.0", "id": self.sequence, "method": method}
        if params is not None:
            payload["params"] = params
        self.process.stdin.write((json.dumps(payload) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 35
        while b"\n" not in self.pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("MCP response timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk or len(self.pending) + len(chunk) > 1024 * 1024:
                raise ValueError("MCP response missing or oversized")
            self.pending.extend(chunk)
        line, _, rest = self.pending.partition(b"\n")
        self.pending = bytearray(rest)
        decoded = json.loads(line)
        if decoded.get("id") != self.sequence or decoded.get("jsonrpc") != "2.0":
            raise ValueError("MCP request returned an unexpected response")
        return decoded

    def request(self, method, params=None):
        decoded = self.request_raw(method, params)
        if decoded.get("id") != self.sequence or decoded.get("jsonrpc") != "2.0" or "error" in decoded:
            raise ValueError("MCP request failed or returned an unexpected response")
        return decoded["result"]


def contract_ac01(client, fixture):
    """Exercise advertised arguments without claiming synthetic results as live acceptance."""
    if set(fixture) != {"query", "code", "language"} or not all(
        isinstance(value, str) and value for value in fixture.values()
    ):
        raise ValueError("isolated search and interpreter fixture required")
    listing = client.request("tools/list")
    required = {"websearch___search": ["query"], "code_interpreter___start": [],
                "code_interpreter___execute": ["session_id", "code", "language", "execution_ref"],
                "code_interpreter___close": ["session_id", "execution_ref"]}
    schemas = {tool["name"]: tool["inputSchema"] for tool in listing["tools"]}
    for name, fields in required.items():
        schema = schemas[name]
        if (schema.get("x-adp-schema-version") != "1.0" or
                set(schema.get("required", [])) != set(fields) or
                not set(fields) <= set(schema.get("properties", {})) or
                {"request_id", "execution_id", "adp_assertion"} & set(schema.get("properties", {}))):
            raise ValueError("advertised tool schema requires unusable authority")

    def invoke(name, arguments):
        response = client.request("tools/call", {"name": name, "arguments": arguments})
        receipt = json.loads(response["content"][0]["text"])
        if response.get("isError") or receipt.get("status") != "confirmed":
            raise ValueError("tool did not return a confirmed receipt")
        return receipt

    search = invoke("websearch___search", {"query": fixture["query"]})
    start = invoke("code_interpreter___start", {})
    session = start.get("result")
    while isinstance(session, dict) and "session_id" not in session:
        session = session.get("result")
    if not isinstance(session, dict) or not isinstance(session.get("session_id"), str):
        raise ValueError("start did not return a session handle")
    reference = str(UUID(start["operation_ref"]))
    binding = {"execution_ref": reference, "session_id": session["session_id"]}
    try:
        execute = invoke("code_interpreter___execute", {**binding, "code": fixture["code"],
                                                          "language": fixture["language"]})
    finally:
        close = invoke("code_interpreter___close", binding)
    return {"request_ids": [str(UUID(receipt["operation_ref"])) for receipt in (search, start, execute, close)],
            "execution_id": str(UUID(start["execution_id"])), "close_receipt_status": close["status"],
            "receipt_ids": [receipt.get("receipt_id") for receipt in (search, start, execute, close)],
            "cleanup": {"status": "not-verified", "provider_termination": None}}


def interpreter_ac01(client, fixture, observer, *, ttl_seconds=300):
    if (type(ttl_seconds) is not int or not 0 < ttl_seconds <= 300 or
            not isinstance(fixture, dict) or fixture.get("path") != "/tmp/adp-8070-result.txt"
            or not callable(getattr(observer, "provider_calls", None))
            or not callable(getattr(observer, "session_status", None))):
        raise FixtureUnavailable("isolated interpreter fixture and independent provider observer required")
    expected = b"42\n"
    code = "from pathlib import Path; print(6*7); Path('/tmp/adp-8070-result.txt').write_bytes(b'42\\n')"
    before = observer.provider_calls()

    def call(name, arguments):
        response = client.request("tools/call", {"name": name, "arguments": arguments})
        if len(response.get("content", [])) != 1:
            raise ValueError("interpreter operation receipt is missing")
        receipt = json.loads(response["content"][0]["text"])
        for _ in range(4):
            if receipt.get("status") == "confirmed" and not response.get("isError"):
                return receipt
            if receipt.get("status") not in {"pending", "unknown"} or not receipt.get("operation_ref"):
                raise ValueError("interpreter operation did not confirm")
            response = client.request("tools/call", {"name": "adp___operation_status", "arguments": {
                "operation_ref": receipt["operation_ref"]}})
            receipt = json.loads(response["content"][0]["text"])
        raise ValueError("interpreter operation did not settle in bounded polling")

    start = call("code_interpreter___start", {"ttl_seconds": ttl_seconds})
    session = start["result"]["result"]["result"]
    reference = start["operation_ref"]
    binding = {"execution_ref": reference, "session_id": session["session_id"]}
    try:
        execute = call("code_interpreter___execute", {**binding, "language": "python", "code": code})
        task = execute["result"]["result"]["result"]
        result = call("code_interpreter___result", {**binding, "provider_id": task["provider_id"]})
        outcome = result["result"]["result"]["result"]["result"]
        if (outcome.get("stdout", "").strip() != "42" or outcome.get("exitCode") != 0
                or result.get("execution_id") != start.get("execution_id")):
            raise ValueError("interpreter stdout, exit code or owner binding differed")
        file = call("code_interpreter___file", {**binding, "path": fixture["path"]})
        reading = client.request("tools/call", {"name": "adp___artifact_read", "arguments": {
            "operation_ref": file["operation_ref"]}})
        if reading.get("isError") or len(reading.get("content", [])) != 1:
            raise ValueError("interpreter file read failed")
        chunk = json.loads(reading["content"][0]["text"])
        data = base64.b64decode(chunk["data_base64"], validate=True)
        if (data != expected or chunk.get("sha256") != hashlib.sha256(expected).hexdigest()
                or file.get("execution_id") != start.get("execution_id")):
            raise ValueError("interpreter file bytes or owner binding differed")
        observation = {"execution_id": start["execution_id"], "result_ref": result["operation_ref"],
                       "file_sha256": chunk["sha256"], "stdout": outcome["stdout"].strip(),
                       "exitCode": outcome["exitCode"]}
    finally:
        closed = call("code_interpreter___close", binding)
        if not closed["result"]["result"]["result"].get("termination_confirmed") or observer.session_status() != "TERMINATED":
            raise ValueError("interpreter provider termination unconfirmed")
    return {**observation, "provider_calls": observer.provider_calls() - before}


def interpreter_ac02(driver, observer):
    if (driver is None or getattr(driver, "isolated", None) is not True
            or getattr(driver, "authorized", None) is not True
            or not callable(getattr(observer, "provider_calls", None))
            or not callable(getattr(observer, "session_status", None))
            or not all(callable(getattr(driver, name, None)) for name in
                       ("foreign", "traversal", "symlink", "expired", "nonzero", "cleanup"))):
        raise FixtureUnavailable("isolated interpreter denial fixture and provider observer required")
    before = observer.provider_calls()
    try:
        for method, status in (("foreign", 404), ("traversal", 422), ("expired", 403)):
            found = getattr(driver, method)()
            if found.get("status") != status or observer.provider_calls() != before:
                raise ValueError("rejected interpreter operation reached provider")
        link = driver.symlink()
        if (link.get("status") != "failed" or link.get("artifact") is not None
                or observer.provider_calls() - before != 1):
            raise ValueError("symlink file escaped provider sandbox")
        failed = driver.nonzero()
        if (failed.get("status") != "failed" or failed.get("exitCode") != 2
                or observer.provider_calls() - before != 3):
            raise ValueError("nonzero interpreter execution misreported")
        return {"denials": {"foreign": 404, "traversal": 422, "expired": 403},
                "nonzero_exit": 2, "observed_provider_calls": 3}
    finally:
        driver.cleanup()
        if observer.session_status() != "TERMINATED":
            raise ValueError("interpreter cleanup unconfirmed")


def interpreter_ac03(driver, observer):
    if (driver is None or getattr(driver, "isolated", None) is not True
            or getattr(driver, "authorized", None) is not True
            or not callable(getattr(observer, "execute_calls", None))
            or not callable(getattr(observer, "session_status", None))
            or not all(callable(getattr(driver, name, None)) for name in
                       ("lose_execute_reply", "recover_same_reference", "read_owned_file", "cleanup"))):
        raise FixtureUnavailable("isolated interpreter recovery fixture and provider observer required")
    before = observer.execute_calls()
    try:
        lost = driver.lose_execute_reply()
        if (lost.get("status") != "unknown" or not lost.get("operation_ref")
                or observer.execute_calls() - before != 1):
            raise ValueError("uncertain interpreter execute was not saved once")
        recovered = driver.recover_same_reference(lost["operation_ref"])
        if (recovered.get("status") != "confirmed" or recovered.get("operation_ref") != lost["operation_ref"]
                or recovered.get("stdout", "").strip() != "42" or recovered.get("exitCode") != 0
                or observer.execute_calls() - before != 1):
            raise ValueError("recovery replayed execute or lost the original result")
        owned = driver.read_owned_file()
        content = base64.b64decode(owned["data_base64"], validate=True)
        if (content != b"42\n" or owned.get("sha256") != hashlib.sha256(content).hexdigest()
                or observer.execute_calls() - before != 1):
            raise ValueError("recovery lost the owner-readable artifact")
        evidence = {"operation_ref": lost["operation_ref"], "file_sha256": owned["sha256"],
                    "observed_execute_calls": 1}
    finally:
        cleanup = driver.cleanup()
        if (cleanup.get("status") != "confirmed" or type(cleanup.get("elapsed_seconds")) not in (int, float)
                or not 0 <= cleanup["elapsed_seconds"] <= 600 or observer.session_status() != "TERMINATED"):
            raise ValueError("provider termination was not independently confirmed within 600 seconds")
    return {**evidence, "cleanup_seconds": cleanup["elapsed_seconds"]}


def validate_interpreter_ac04_fixture(client_name, fixture):
    if (client_name not in CLIENTS or not isinstance(fixture, dict) or
            set(fixture) != {"positive", "client_version", "pricing"} or
            fixture["positive"] != {"path": "/tmp/adp-8070-result.txt"} or
            not isinstance(fixture["client_version"], str) or not fixture["client_version"] or
            not isinstance(fixture["pricing"], dict)):
        raise FixtureUnavailable("bounded per-client interpreter fixture required")
    pricing = fixture["pricing"]
    if (pricing.get("authority_confirmed") is not True or
            pricing.get("arguments") != fixture["positive"] or pricing.get("setup") != [] or
            type(pricing.get("max_seconds")) is not int or
            not 0 < pricing["max_seconds"] <= 300 or
            (client_name != "cli" and (not isinstance(pricing.get("all_in_quote"), dict) or
                                       pricing["all_in_quote"].get("max_model_requests", 0) < 6)) or
            type(pricing.get("session_ttl_seconds")) is not int or
            not 0 < pricing["session_ttl_seconds"] <= 300 or
            type(pricing.get("cleanup_seconds")) is not int or
            not 0 < pricing["cleanup_seconds"] <= 600 or
            type(pricing.get("expected_provider_calls")) is not int or
            not 5 <= pricing["expected_provider_calls"] <= 12 or
            type(pricing.get("unit_cost_usd")) not in (int, float) or
            not 0 < pricing["unit_cost_usd"] <= 20 or
            type(pricing.get("max_cost_usd")) not in (int, float) or
            not 0 < pricing["max_cost_usd"] <= 20 or
            pricing["expected_provider_calls"] * pricing["unit_cost_usd"] > pricing["max_cost_usd"]):
        raise FixtureUnavailable("priced and authorized interpreter session with 300/600-second limits required")
    return reserve_all_in(pricing, execution_kind="stdio" if client_name == "cli" else "model")


def interpreter_ac04(client_name, fixture, adapter, observer):
    reservation = validate_interpreter_ac04_fixture(client_name, fixture)
    pricing = fixture["pricing"]
    if (adapter is None or observer is None or
            not callable(getattr(observer, "observe", None)) or
            not callable(getattr(observer, "session_status", None)) or
            not callable(getattr(observer, "provider_calls", None))):
        raise FixtureUnavailable("pinned client and independent provider readback required")
    with adapter as client:
        started = time.monotonic()
        if getattr(client, "version", None) != fixture["client_version"]:
            raise ValueError("installed client version differs from pinned fixture")
        listing = {tool["name"]: tool["inputSchema"] for tool in client.request("tools/list")["tools"]}
        required = {"code_interpreter___start", "code_interpreter___execute", "code_interpreter___result",
                    "code_interpreter___file", "code_interpreter___close", "adp___artifact_read"}
        if not required <= listing.keys():
            raise ValueError("installed client does not advertise the full interpreter lifecycle")
        result = interpreter_ac01(client, fixture["positive"], observer,
                                  ttl_seconds=pricing["session_ttl_seconds"])
        if time.monotonic() - started > pricing["max_seconds"]:
            raise ValueError("installed interpreter client exceeded the 300-second run bound")
        if (str(UUID(result["execution_id"])) != result["execution_id"] or
                str(UUID(result["result_ref"])) != result["result_ref"] or
                not 5 <= result["provider_calls"] <= pricing["expected_provider_calls"]):
            raise ValueError("interpreter reference or provider calls differ from priced fixture")
        readback = observer.observe(execution_id=result["execution_id"],
                                    timeout=pricing["cleanup_seconds"])
        if (not isinstance(readback, dict) or readback.get("source") != "provider-control-plane" or
                readback.get("execution_id") != result["execution_id"] or
                readback.get("status") != "TERMINATED" or
                readback.get("file_sha256") != result["file_sha256"] or
                readback.get("stdout", "").strip() != "42" or readback.get("exitCode") != 0 or
                readback.get("session_count") != 1 or
                type(readback.get("session_ttl_seconds")) is not int or
                not 0 < readback["session_ttl_seconds"] <= pricing["session_ttl_seconds"] or
                type(readback.get("cleanup_elapsed_seconds")) not in (int, float) or
                not 0 <= readback["cleanup_elapsed_seconds"] <= pricing["cleanup_seconds"] or
                type(readback.get("provider_request_count")) is not int or
                not 5 <= readback["provider_request_count"] <= pricing["expected_provider_calls"] or
                type(readback.get("cost_usd")) not in (int, float) or
                not 0 <= readback["cost_usd"] <= pricing["max_cost_usd"]):
            raise ValueError("provider result, session, cost or termination readback differs from client evidence")
    return {"client_version": fixture["client_version"], "result": result,
            "provider_observation": readback, "estimated_all_in_usd": str(reservation["reserved_usd"]),
            "pricing_source_revision": reservation["pricing_source_revision"],
            "cleanup": {"status": "confirmed", "provider_termination": "TERMINATED"}}


def search_ac01(client, fixture, observer):
    if (not isinstance(fixture, dict) or set(fixture) !=
            {"query", "max_results", "expected_urls", "unit_cost_usd", "pricing_source"} or
            not isinstance(fixture["query"], str) or not fixture["query"].strip() or
            type(fixture["max_results"]) is not int or not 1 <= fixture["max_results"] <= 25 or
            not isinstance(fixture["expected_urls"], list) or
            not fixture["expected_urls"] or len(fixture["expected_urls"]) > fixture["max_results"] or
            any(not isinstance(url, str) or not url.startswith("https://") for url in fixture["expected_urls"]) or
            type(fixture["unit_cost_usd"]) not in (int, float) or
            not 0 < fixture["unit_cost_usd"] <= 20 or
            not isinstance(fixture["pricing_source"], str) or not fixture["pricing_source"] or
            not callable(getattr(observer, "provider_calls", None))):
        raise FixtureUnavailable("bounded search fixture and independent provider counter required")
    listed = {tool["name"]: tool["inputSchema"] for tool in client.request("tools/list")["tools"]}
    schema = listed["websearch___search"]
    if (schema.get("required") != ["query"] or
            set(schema.get("properties", {})) != {"query"} or
            schema.get("additionalProperties") is not False):
        raise ValueError("search was not publicly advertised with bounded options")
    before = observer.provider_calls()
    response = client.request("tools/call", {"name": "websearch___search", "arguments": {
        "query": fixture["query"]}})
    if response.get("isError") or len(response.get("content", [])) != 1:
        raise ValueError("search did not confirm")
    receipt = json.loads(response["content"][0]["text"])
    if receipt.get("status") != "confirmed" or not receipt.get("receipt_id"):
        raise ValueError("search receipt not attributable and confirmed")
    result = receipt["result"]["result"]["result"]["result"]
    urls = [item["url"] for item in result["results"]]
    if (urls != fixture["expected_urls"] or len(urls) > fixture["max_results"] or
            result.get("citations") != urls or
            result.get("query_count") != 1 or
            result.get("estimated_search_usd") != fixture["unit_cost_usd"] or
            result.get("pricing_source") != fixture["pricing_source"] or
            observer.provider_calls() - before != 1):
        raise ValueError("search results, citations, estimate or dispatch count differ from independent fixture")
    return {"receipt_id": receipt["receipt_id"], "result_urls": urls,
            "operation_ref": receipt.get("operation_ref"), "execution_id": receipt.get("execution_id"),
            "query_count": 1, "estimated_search_usd": result["estimated_search_usd"],
            "observed_provider_calls": 1}


def search_ac02(driver, observer):
    if (driver is None or not callable(getattr(observer, "provider_calls", None)) or
            not all(callable(getattr(driver, name, None)) for name in
                    ("denied", "malformed", "throttled", "retry", "changed_payload"))):
        raise FixtureUnavailable("isolated search refusal fixture and independent provider counter required")
    before = observer.provider_calls()
    for method, expected in (("denied", 403), ("malformed", 422)):
        if getattr(driver, method)().get("status") != expected or observer.provider_calls() != before:
            raise ValueError("unauthorized or malformed search dispatched")
    throttled = driver.throttled()
    if (throttled.get("status") != 202 or not throttled.get("request_id") or
            type(throttled.get("retry_after_seconds")) is not int or
            not 0 < throttled["retry_after_seconds"] <= 86400 or
            observer.provider_calls() - before != 1):
        raise ValueError("provider throttle did not preserve uncertain request and bounded retry guidance")
    repeated = driver.retry(throttled["request_id"])
    changed = driver.changed_payload(throttled["request_id"])
    if (repeated != throttled or changed.get("status") != 409 or
            observer.provider_calls() - before != 1):
        raise ValueError("same request or changed payload repeated a paid search")
    return {"request_id": throttled["request_id"], "retry_after_seconds": throttled["retry_after_seconds"],
            "denied": 403, "malformed": 422, "changed_payload": 409, "observed_provider_calls": 1}


def search_ac04(client_name, fixture, adapter, observer):
    if (client_name not in CLIENTS or not isinstance(fixture, dict) or
            set(fixture) != {"positive", "pricing", "client_version"} or
            not isinstance(fixture["positive"], dict) or
            not isinstance(fixture["client_version"], str) or not fixture["client_version"] or
            not isinstance(fixture["pricing"], dict)):
        raise FixtureUnavailable("isolated per-client search matrix and priced fixture required")
    pricing = fixture["pricing"]
    positive = fixture["positive"]
    reservation = validate_fixture(pricing, client_name=client_name)
    if (pricing["arguments"] != {"query": positive.get("query")} or pricing["setup"] or
            pricing["expected_provider_calls"] != 2 or
            pricing["unit_cost_usd"] != positive.get("unit_cost_usd") or
            pricing["pricing_source"] != positive.get("pricing_source") or
            (client_name != "cli" and pricing["all_in_quote"]["max_model_requests"] < 7)):
        raise ValueError("search matrix differs from priced provider and native-client work")
    if adapter is None or observer is None or not callable(getattr(observer, "observe", None)):
        raise FixtureUnavailable("pinned client and independent provider readback required")
    with adapter as client:
        if getattr(client, "version", None) != fixture["client_version"]:
            raise ValueError("installed client version differs from pinned fixture")
        result = search_ac01(client, positive, observer)
        if (not isinstance(result["operation_ref"], str) or
                str(UUID(result["operation_ref"])) != result["operation_ref"] or
                not isinstance(result["execution_id"], str) or
                str(UUID(result["execution_id"])) != result["execution_id"]):
            raise ValueError("positive search receipt has no owned operation and execution references")
        if not callable(getattr(client, "close_original", None)):
            raise FixtureUnavailable("original search closure unavailable")
        try:
            negative = search_ac02(client, observer)
        finally:
            closure = client.close_original(result["operation_ref"])
        if closure.get("cleanup_status") != "confirmed":
            raise ValueError("confirmed search execution did not close")
        readback = verify_readback(result, pricing, observer, operation="websearch___search", client=client_name)
    return {"client_version": fixture["client_version"], "positive": result,
            "negative": negative, "provider_observation": readback,
            "estimated_all_in_usd": str(reservation["reserved_usd"]),
            "pricing_source_revision": reservation["pricing_source_revision"],
            "cleanup": readback["cleanup"]}


def contract_ac02(client, fixture):
    if (set(fixture) != {"foreign_ref", "foreign_session"} or
            not isinstance(fixture["foreign_ref"], str) or
            not isinstance(fixture["foreign_session"], str) or not fixture["foreign_session"] or
            str(UUID(fixture["foreign_ref"])) != fixture["foreign_ref"]):
        raise ValueError("isolated foreign-handle fixture required")
    cases = [
        ({"name": "websearch___search", "arguments": {"query": "isolated search"},
          "request_id": fixture["foreign_ref"]}, ("Tool name and object arguments required",)),
        ({"name": "websearch___search", "arguments": {"query": "isolated search",
          "adp_assertion": "untrusted"}}, ("Server-owned authority arguments are not accepted",)),
        ({"name": "code_interpreter___execute", "arguments": {"execution_ref": fixture["foreign_ref"],
          "session_id": fixture["foreign_session"], "code": "print(1)", "language": "python"}},
         ("Unknown operation reference", "Execution reference belongs to a different login")),
        ({"name": "websearch___search", "arguments": {"query": 42}},
         ("Arguments do not match the advertised client schema",)),
    ]
    for params, messages in cases:
        packet = client.request_raw("tools/call", params)
        error = packet.get("error", {})
        if (packet.get("jsonrpc") != "2.0" or error.get("code") != -32603 or
                not any(message in error.get("message", "") for message in messages) or "result" in packet):
            raise ValueError("invalid call was not rejected before dispatch")
    return {"rejected_calls": len(cases), "provider_dispatch": "not-verified"}


def contract_ac03(client, client_factory, command, fault_driver, fixture):
    if (set(fixture) != {"query", "changed_query"} or
            not all(isinstance(value, str) and value for value in fixture.values()) or
            fixture["query"] == fixture["changed_query"]):
        raise ValueError("isolated lost-reply fixture required")
    before = fault_driver.provider_calls()
    fault_driver.arm_drop()
    try:
        client.request("tools/call", {"name": "websearch___search", "arguments": {"query": fixture["query"]}})
    except TimeoutError:
        pass
    else:
        raise ValueError("reply-loss injection did not occur")

    with client_factory(command) as recovered:
        listed = recovered.request("tools/call", {"name": "adp___operations_list", "arguments": {}})
        operations = json.loads(listed["content"][0]["text"])["operations"]
        if len(operations) != 1 or operations[0]["tool"] != "websearch___search":
            raise ValueError("original operation was not retained in the isolated store")
        reference = str(UUID(operations[0]["operation_ref"]))
        if operations[0]["status"] != "unknown":
            raise ValueError("lost reply did not preserve uncertain operation")

        def retry(query):
            response = recovered.request("tools/call", {"name": "adp___operation_retry", "arguments": {
                "operation_ref": reference, "tool": "websearch___search", "arguments": {"query": query}}})
            return json.loads(response["content"][0]["text"])

        conflict = retry(fixture["changed_query"])
        if conflict.get("status") != "rejected" or conflict.get("operation_ref") != reference:
            raise ValueError("changed payload did not conflict with original operation")
        receipt = retry(fixture["query"])
        if receipt.get("status") != "confirmed" or receipt.get("operation_ref") != reference:
            raise ValueError("original operation could not be recovered")
    if fault_driver.provider_calls() - before != 1:
        raise ValueError("provider action was repeated after reconnect")
    return {"request_id": reference, "status": "confirmed", "changed_payload": "rejected",
            "observed_provider_calls": 1, "provider_dispatch": "not-independently-verified",
            "cleanup": {"status": "not-applicable", "reason": "read-only search"}}


def validate_contract_ac04_fixture(fixture):
    fields = {"query", "code", "language", "client_version", "max_seconds", "cleanup_seconds",
              "session_ttl_seconds", "max_cost_usd", "unit_cost_usd", "pricing_source", "authority_confirmed"}
    if not isinstance(fixture, dict) or set(fixture) != fields:
        raise FixtureUnavailable("private client, pricing and authority fixture required")
    if (fixture["authority_confirmed"] is not True or
            any(not isinstance(fixture[name], str) or not fixture[name] for name in
                ("query", "code", "language", "client_version", "pricing_source")) or
            type(fixture["max_seconds"]) is not int or not 0 < fixture["max_seconds"] <= 300 or
            type(fixture["session_ttl_seconds"]) is not int or not 0 < fixture["session_ttl_seconds"] <= 300 or
            type(fixture["cleanup_seconds"]) is not int or not 0 < fixture["cleanup_seconds"] <= 600 or
            any(type(fixture[name]) not in {int, float} or not math.isfinite(fixture[name]) or fixture[name] <= 0 for name in
                ("max_cost_usd", "unit_cost_usd")) or
            fixture["max_cost_usd"] > 20 or fixture["unit_cost_usd"] * 4 > fixture["max_cost_usd"]):
        raise FixtureUnavailable("confirmed authority and priced bounded fixture required")


def contract_ac04(client, fixture, observer, *, live=False, clock=time.monotonic):
    validate_contract_ac04_fixture(fixture)
    if getattr(client, "version", None) != fixture["client_version"]:
        raise ValueError("pinned client version does not match fixture")
    started = clock()
    result = contract_ac01(client, {key: fixture[key] for key in ("query", "code", "language")})
    if clock() - started > fixture["max_seconds"]:
        raise ValueError("stateful fixture exceeded its bounded duration")
    if not all(isinstance(receipt_id, str) and receipt_id for receipt_id in result["receipt_ids"]):
        raise ValueError("client did not retain authorization receipts")
    if not live or observer is None or not callable(getattr(observer, "observe", None)):
        return "blocked", "independent provider readback and cleanup unavailable", result
    try:
        observed = observer.observe(execution_id=result["execution_id"],
                                    request_ids=tuple(result["request_ids"]),
                                    timeout=fixture["cleanup_seconds"])
    except (FixtureUnavailable, ValueError, TimeoutError, OSError, KeyError):
        return "blocked", "independent provider readback unavailable", result
    if (not isinstance(observed, dict) or
            any(not isinstance(observed.get(name), list) or
                any(not isinstance(value, str) for value in observed[name])
                for name in ("request_ids", "receipt_ids"))):
        return "blocked", "independent provider readback unavailable", result
    if (observed.get("source") != "provider-control-plane" or
            observed.get("execution_id") != result["execution_id"] or
            set(observed.get("request_ids", [])) != set(result["request_ids"]) or
            set(observed.get("receipt_ids", [])) != set(result["receipt_ids"])):
        return "blocked", "provider readback does not match client receipts", result
    if observed.get("termination") == "failed" or observed.get("cleanup_status") == "failed":
        return "fail", "provider cleanup failed", result
    if (observed.get("termination") != "stopped" or observed.get("cleanup_status") != "confirmed" or
            not observed.get("termination_observed_at") or
            type(observed.get("provider_request_count")) is not int or
            type(observed.get("session_count")) is not int or
            type(observed.get("session_ttl_seconds")) is not int or
            type(observed.get("cleanup_elapsed_seconds")) not in {int, float} or
            type(observed.get("cost_usd")) not in {int, float}):
        return "blocked", "independent provider count, session, cost or cleanup missing", result
    if (observed["provider_request_count"] != 4 or observed["session_count"] != 1 or
            not 0 < observed["session_ttl_seconds"] <= fixture["session_ttl_seconds"] or
            not 0 <= observed["cleanup_elapsed_seconds"] <= fixture["cleanup_seconds"] or
            not 0 <= observed["cost_usd"] <= fixture["max_cost_usd"]):
        return "fail", "provider calls, session, cleanup or cost exceeded bounds", result
    result["cleanup"] = {"status": "confirmed", "provider_termination": "stopped"}
    return "pass", "independent provider requests and cleanup confirmed", result


def release_ac01(fixture):
    """Test release selection against an isolated independent oracle, never a live claim."""
    if not isinstance(fixture, dict):
        return "blocked", "isolated release fixture unavailable", {}
    source = fixture.get("source_revision")
    run_id = fixture.get("run_id")
    attempt = fixture.get("run_attempt")
    if not (isinstance(source, str) and REVISION.fullmatch(source)
            and type(run_id) is int and run_id > 0 and type(attempt) is int and attempt > 0
            and isinstance(fixture.get("paths"), list) and isinstance(fixture.get("expected_steps"), list)
            and fixture.get("provider_request_count") == 0):
        return "blocked", "source, run, attempt or independent provider counter unavailable", {}
    steps = operator_sequence(fixture["paths"])
    observations = {"source_revision": source, "run_id": run_id, "run_attempt": attempt,
                    "provider_request_count": 0, "steps": steps, "evidence_type": "isolated-fixture",
                    "cleanup": {"status": "not-required", "provider_termination": "not-started"}}
    if fixture.get("mode") != "plan" or fixture.get("claimed_runtime") is True:
        return "fail", "a plan or unresolved dispatch cannot prove deployed runtime", observations
    if steps != fixture["expected_steps"]:
        return "fail", "component or workflow selection differs from independent oracle", observations
    if any(step["component"] == "shared Lambda and Browser" and step["trigger"] != "dispatch apply"
           for step in steps):
        return "fail", "shared provider must use explicit protected apply", observations
    return "pass", "selected reviewed workflows; runtime was not claimed", observations


def release_ac02(fixture):
    """Reconcile isolated run and receipt snapshots without dispatching a workflow."""
    if not isinstance(fixture, dict) or fixture.get("provider_request_count") != 0:
        return "blocked", "independent provider counter or release fixture unavailable", {}
    try:
        ancestors = set(tuple(pair) for pair in fixture["ancestry"])
        result = reconcile(operator_sequence(fixture["paths"]), fixture["source_revision"],
                           fixture["main_revision"], fixture["manifest"], fixture["runs"],
                           fixture["receipts"], lambda older, newer: older == newer or (older, newer) in ancestors,
                           environment=fixture["environment"], account=fixture["account"])
        decisions = [{key: decision.get(key) for key in ("component", "action", "source_revision",
                                                         "run_id", "run_attempt")}
                     for decision in result["decisions"]]
    except (KeyError, TypeError, ValueError):
        return "blocked", "observed run, receipt or main ancestry unavailable", {}
    observations = {"source_revision": fixture["source_revision"],
                    "main_revision": fixture["main_revision"], "decisions": decisions,
                    "provider_request_count": 0, "evidence_type": "isolated-fixture",
                    "cleanup": {"status": "not-required", "provider_termination": "not-started"}}
    if decisions != fixture.get("expected_decisions") or any(
            decision["action"] != "reuse" for decision in decisions):
        return "fail", "unverified, duplicate or newer incompatible release was not safely reconciled", observations
    return "pass", "compatible observed release receipts reused without dispatch", observations


def release_ac03(fixture):
    """Inspect an isolated saved attempt and independently matched resume receipt."""
    if not isinstance(fixture, dict) or fixture.get("provider_request_count") != 0:
        return "blocked", "isolated journal and independent provider counter unavailable", {}
    try:
        prerequisites(fixture["selected"], fixture["bindings"])
        journal = load_journal(fixture["journal_path"])
        stage = fixture["resume_stage"]
        attempt = journal["stages"][stage]
        if (journal["source"] != fixture["source_revision"]
                or attempt != fixture["expected_attempt"]
                or fixture["next_stage"] in journal["stages"]):
            return "fail", "saved attempt or untouched next stage differs from independent oracle", {}
        action = journal_decision(fixture["journal_path"], stage, bindings=fixture["bindings"],
                                  resume=True, receipts_dir=fixture["receipts_dir"])
        next_action = journal_decision(fixture["journal_path"], fixture["next_stage"],
                                       bindings=fixture["bindings"], resume=True)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return "blocked", str(exc), {}
    observations = {"source_revision": journal["source"], "run_id": attempt["run_id"],
                    "run_attempt": attempt["run_attempt"], "resume_action": action,
                    "next_action": next_action, "evidence_type": "isolated-fixture",
                    "provider_request_count": 0,
                    "cleanup": {"status": "not-required", "provider_termination": "not-started"}}
    if action != "skip" or next_action != "run":
        return "fail", "resume skipped an unverified stage or repeated a verified one", observations
    return "pass", "only independently receipted stages can resume", observations



def release_ac04(fixture, *, readback=None, source, account, gateway_image, facade_image, live=False):
    if live and (not isinstance(fixture, dict) or readback is None):
        return "blocked", "Coordinator: supply the approved private component manifest and authorized independent runtime observer", {}
    if not live:
        if not isinstance(fixture, dict) or fixture.get("provider_request_count") != 0:
            return "blocked", "isolated component readbacks and zero-provider-call counter unavailable", {}
        readback = lambda phase: fixture["readbacks"][phase]
    try:
        observations = verify_window(fixture, readback, source=source, account=account,
                                     gateway_image=gateway_image, facade_image=facade_image)
    except (KeyError, TypeError, ValueError) as exc:
        return "fail", str(exc), {}
    observations["evidence_type"] = "independent-runtime" if live else "isolated-fixture"
    observations["provider_request_count"] = 0 if not live else None
    return "pass", "selected runtime images remained pinned through evaluation handoff", observations


def verify_gateway(url):
    if not url.startswith("https://"):
        raise ValueError("HTTPS gateway required")
    with urlopen(url.rstrip("/") + "/api/health", timeout=15) as response:
        if json.load(response).get("status") != "healthy":
            raise ValueError("deployed gateway is not healthy")


@contextmanager
def client_runtime_window(manifest, readback, *, source, account, gateway_image, facade_image):
    expected = verify_manifest(manifest, source=source, account=account,
                               gateway_image=gateway_image, facade_image=facade_image)
    if readback("before") != expected:
        raise ValueError("independent runtime changed before installed client invocation")
    try:
        yield {"source_revision": source, "components": sorted(expected),
               "receipts": {name: {"source_revision": entry["source_revision"],
                                   "run_id": entry["receipt"]["run_id"],
                                   "run_attempt": entry["receipt"]["run_attempt"]}
                            for name, entry in manifest["components"].items()}}
    finally:
        if readback("after") != expected:
            raise ValueError("independent runtime changed during installed client invocation")


CLIENT_EVIDENCE_FIELDS = frozenset({
    "source", "client_version", "mcp_version", "discovery_sha256", "installed_cli_sha256",
    "installed_mcp_sha256", "operation_ref", "receipt_id", "receipt_sha256", "execution_id",
    "artifact_sha256", "artifact_chunk_sha256", "artifact_offset", "artifact_length",
    "artifact_total_length", "model_requests", "input_tokens", "output_tokens",
    "estimated_all_in_usd", "pricing_source_revision", "provider_request_count", "session_count",
    "session_ttl_seconds", "cleanup_elapsed_seconds", "cost_usd", "status", "observed_at",
    "operation", "passed", "source_revision", "run_id", "run_attempt",
    "provider_observation", "assertion", "cleanup", "faults", "missing_fixture",
    "changed_digest", "timeout", "unconfirmed_cleanup",
    "operation_id", "content_sha256", "effect_count", "execution", "provider_fixtures",
})
SECRET_TEXT = re.compile(r"(?i)(?:bearer\s+|authorization|password|secret|token|api[_-]?key|credential|https?://\S*@)")


def client_evidence_value(value):
    if isinstance(value, dict):
        return {key: client_evidence_value(item) for key, item in value.items()
                if key in CLIENT_EVIDENCE_FIELDS and (not SECRET_TEXT.search(key) or
                (key in {"input_tokens", "output_tokens"} and type(item) is int and item >= 0))}
    if isinstance(value, (list, tuple)):
        return [client_evidence_value(item) for item in value]
    if isinstance(value, str):
        return value if len(value) <= 256 and not SECRET_TEXT.search(value) else "[redacted]"
    return value if value is None or type(value) in (int, float, bool) else "[redacted]"


def client_evidence_row(row, report):
    allowed = {"scenario_id", "cell_id", "tags", "client", "tool", "client_version", "status",
               "issue_number", "local_scenario_id", "evidence_scope", "reason", "recovery_id",
               "request_ids", "cleanup", "observations"}
    record = {key: value for key, value in row.items() if key in allowed}
    for key in ("client_version", "reason"):
        if key in record:
            record[key] = client_evidence_value(record[key])
    if "request_ids" in record:
        record["request_ids"] = [client_evidence_value(ref) for ref in record["request_ids"]]
    if "observations" in record:
        observation = record["observations"]
        record["observations"] = client_evidence_value(observation)
        if isinstance(observation, dict):
            provider = observation.get("provider_observation")
            assertion = provider.get("assertion") if isinstance(provider, dict) else None
            # Exact public cell identifiers are schema, not credential values.
            # Preserve only the allowlisted operation in its assertion slot;
            # arbitrary text, other fields and mismatched cells stay redacted.
            if (record.get("tool") in OPERATIONS and isinstance(assertion, dict) and
                    assertion.get("operation") == record["tool"]):
                record["observations"]["provider_observation"]["assertion"]["operation"] = record["tool"]
        if isinstance(observation, dict) and isinstance(observation.get("runtime_window"), dict):
            window = observation["runtime_window"]
            record["observations"]["runtime_window"] = {
                "source_revision": client_evidence_value(window.get("source_revision")),
                "components": [name for name in window.get("components", [])
                               if isinstance(name, str) and len(name) <= 100 and not SECRET_TEXT.search(name)],
                "receipts": {name: client_evidence_value(receipt)
                             for name, receipt in window.get("receipts", {}).items()
                             if name in window.get("components", []) and isinstance(name, str)
                             and len(name) <= 100 and not SECRET_TEXT.search(name)},
            }
    if "cleanup" in record:
        record["cleanup"] = client_evidence_value(record["cleanup"])
    invocation = record.get("observations", {})
    if not isinstance(invocation, dict):
        invocation = {}
    provider = invocation.get("provider_observation")
    record["client_evidence"] = {
        "schema": "installed-client-evidence/v1",
        "scenario_id": record["scenario_id"],
        "acceptance_ids": [tag.removeprefix("@acceptance ") for tag in record["tags"]
                           if tag.startswith("@acceptance ")],
        "client": record["client"], "cell_id": record["cell_id"],
        "status": record["status"],
        "source": {"harness_revision": client_evidence_value(report.get("harness_revision")),
                   "gateway_revision": report["deployed_revisions"]["gateway"],
                   "invocation": invocation.get("source")},
        "run": {"id": client_evidence_value(report.get("github_run_id")),
                "attempt": client_evidence_value(report.get("github_run_attempt"))},
        "target": report["deployed_revisions"],
        "provider_source": provider.get("source") if isinstance(provider, dict) else None,
    }
    serialized = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    record["evidence_sha256"] = hashlib.sha256(serialized).hexdigest()
    return record


def write_results(directory, report):
    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    published = {**report, "scenarios": [client_evidence_row(row, report) if row["scenario_id"].startswith("SCN-8067-") or row["scenario_id"] == "SCN-8454-02"
                                        else row for row in report["scenarios"]]}
    (destination / "evidence.json").write_text(json.dumps(published, indent=2, sort_keys=True) + "\n")
    suite = ElementTree.Element("testsuite", name="tools-live-evaluation",
                                 tests=str(len(report["scenarios"])),
                                 failures=str(sum(row["status"] != "pass" for row in report["scenarios"])))
    if report.get("error") and not report["scenarios"]:
        ElementTree.SubElement(ElementTree.SubElement(suite, "testcase", name="scenario-selection"),
                               "failure", message=report["error"])
        suite.set("tests", "1")
        suite.set("failures", "1")
    for row in published["scenarios"]:
        entry = ElementTree.SubElement(suite, "testcase", name=row["scenario_id"] + "[" + row["cell_id"] + "]",
                                       classname=",".join(row["tags"]))
        if row["status"] != "pass":
            ElementTree.SubElement(entry, "failure", message=row["status"]).text = row.get("reason", "")
    ElementTree.ElementTree(suite).write(destination / "junit.xml", encoding="unicode", xml_declaration=True)




def recovery_request_ids(root, recovery_id):
    if not root or not recovery_id or str(UUID(recovery_id)) != recovery_id:
        raise ValueError("run-owned recovery binding missing")
    store = Path(root) / recovery_id / ".bedrock-gateway" / "tool-operations"
    if not store.is_dir() or store.is_symlink():
        return []
    identifiers = []
    for record in store.iterdir():
        if not record.is_file() or record.is_symlink() or not record.name.endswith(".json"):
            raise ValueError("unsafe run-owned recovery record")
        identifier = record.name.removesuffix(".json")
        if str(UUID(identifier)) != identifier:
            raise ValueError("noncanonical original recovery reference")
        identifiers.append(identifier)
    return sorted(identifiers)


def block_uncertain_matrix(row, recovery_root, error):
    row["status"], row["reason"] = "blocked", type(error).__name__
    row["cleanup"] = {"status": "unconfirmed", "provider_termination": None}
    try:
        row["request_ids"] = recovery_request_ids(recovery_root, row["recovery_id"])
    except (ValueError, OSError):
        row["request_ids"] = []
        row["reason"] = "recovery references cannot be inspected"


def recover_original_operation(recovery_root, recovery_id, operation_ref, *, gateway_url,
                               gateway_revision, refresh_file, client_factory=MCPClient):
    if str(UUID(operation_ref)) != operation_ref or operation_ref not in recovery_request_ids(
            recovery_root, recovery_id):
        raise ValueError("owned original operation reference unavailable")
    with resume_installed_clients(gateway_url, gateway_revision, recovery_root=recovery_root,
                                  recovery_id=recovery_id) as (installed, environ, _):
        record_path = Path(environ["BG_CONFIG_DIR"]) / "tool-operations" / (operation_ref + ".json")
        metadata = record_path.lstat()
        if not record_path.is_file() or record_path.is_symlink() or metadata.st_mode & 0o077:
            raise ValueError("original operation record is not private")
        record = json.loads(record_path.read_text())
        if record.get("request_id") != operation_ref or not isinstance(record.get("arguments"), dict):
            raise ValueError("original operation record does not match its reference")
        import_session(installed, environ, refresh_file)
        with client_factory([str(installed), "mcp"], environ=environ) as client:
            response = client.request("tools/call", {"name": "adp___operation_status",
                                                      "arguments": {"operation_ref": operation_ref}})
        content = response.get("content") if isinstance(response, dict) else None
        if not isinstance(content, list) or len(content) != 1 or content[0].get("type") != "text":
            raise ValueError("original operation readback unavailable")
        receipt = json.loads(content[0]["text"])
        if (response.get("isError") or receipt.get("status") != "confirmed" or
                receipt.get("operation_ref") != operation_ref or not receipt.get("receipt_id")):
            raise ValueError("original operation remains unknown or unconfirmed")
        return {"operation_ref": operation_ref, "receipt_id": receipt["receipt_id"],
                "receipt_sha256": hashlib.sha256(content[0]["text"].encode()).hexdigest(),
                "cleanup": {"status": "unconfirmed", "provider_termination": None}}


def harness_ac01(client_name, *, gateway_url, gateway_revision, refresh_file, client_factory=MCPClient):
    if client_name not in CLIENTS:
        raise ValueError("unknown installed client")
    with installed_clients(gateway_url, gateway_revision) as (installed, environ, hashes):
        import_session(installed, environ, refresh_file)
        if client_name != "cli":
            configure_model_transport(installed, environ)
        with client_factory([str(installed), "mcp"], environ=environ) as client:
            discovery = check_discovery(client.request("tools/list"))
            mcp_version = client.version
        client_version = mcp_version
        if client_name != "cli":
            binary = "claude" if client_name == "claude-code" else "codex"
            result = subprocess.run([binary, "--version"], env=environ, cwd=environ["HOME"],
                                    capture_output=True, timeout=15)
            if result.returncode or not result.stdout.strip():
                raise ValueError("fresh native client cannot launch")
            client_version = result.stdout.decode().strip().splitlines()[0][:100]
        return {"source": "verified-installed-cli", "client_version": client_version,
                "mcp_version": mcp_version, "discovery_sha256": discovery,
                "installed_cli_sha256": hashes["adp"], "installed_mcp_sha256": hashes["adp-mcp.py"]}


def require_paid_admission():
    raise FixtureUnavailable("authoritative #8078 operation-keyed admission is unavailable before provider dispatch")


def harness_ac02(client_name, operation, fixture, *, gateway_url, gateway_revision,
                 refresh_file, observer, recovery_root, recovery_id, models=None, client_factory=MCPClient):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        raise FixtureUnavailable("paid installed-client operations are forbidden in CI")
    require_paid_admission()


def _offline_harness_ac02(client_name, operation, fixture, *, gateway_url, gateway_revision,
                          refresh_file, observer, recovery_root, recovery_id, offline_installer,
                          client_factory, models=None):
    """Exercise fake MCP adapters only; never called by the protected evaluation path."""
    reservation = validate_fixture(fixture, client_name=client_name)
    if observer is None:
        raise FixtureUnavailable("independent provider readback unavailable")
    if client_name not in CLIENTS or operation not in OPERATIONS:
        raise ValueError("unregistered laptop operation cell")
    if not recovery_root or not recovery_id:
        raise FixtureUnavailable("private durable recovery home required before provider work")
    with offline_installer(gateway_url, gateway_revision, recovery_root=recovery_root,
                           recovery_id=recovery_id) as (installed, environ, hashes):
        import_session(installed, environ, refresh_file)
        if client_name != "cli":
            configure_model_transport(installed, environ)
        owned = prepare_owned(installed, environ, fixture["setup"], client_factory=client_factory)
        arguments = resolve_arguments(fixture["arguments"], owned)
        if client_name == "cli":
            result = cli_call(installed, environ, operation, arguments, client_factory=client_factory, owned=owned)
        else:
            with client_factory([str(installed), "mcp"], environ=environ) as discovery_client:
                discovery = check_discovery(discovery_client.request("tools/list"))
            result = native_call(client_name, installed, environ, operation, arguments,
                                 model=(models or {}).get(client_name), timeout=fixture["max_seconds"],
                                 bounds=reservation["limits"], owned=owned)
            result["discovery_sha256"] = discovery
        readback = verify_readback(result, fixture, observer, operation=operation, client=client_name)
        return {**result, "source": "offline-synthetic-backend",
                "estimated_all_in_usd": str(reservation["reserved_usd"]),
                "pricing_source_revision": reservation["pricing_source_revision"],
                "installed_cli_sha256": hashes["adp"],
                "installed_mcp_sha256": hashes["adp-mcp.py"],
                "provider_observation": readback, "cleanup": readback["cleanup"]}


def confirmed_cleanup(observation):
    cleanup = observation.get("cleanup")
    if (not isinstance(cleanup, dict) or cleanup.get("status") != "confirmed" or
            not cleanup.get("observed_at") or
            observation.get("provider_observation", {}).get("cleanup") != cleanup):
        raise ValueError("independent provider cleanup is not confirmed")
    return cleanup


def harness_ac03(*, manifest, readback, source, account, gateway_image, facade_image):
    """Exercise safety failures without launching a client or provider operation."""
    if manifest is None or not callable(readback) or not source or not account:
        raise FixtureUnavailable("approved release fixture and independent runtime observer unavailable")
    with client_runtime_window(manifest, readback, source=source, account=account,
                               gateway_image=gateway_image, facade_image=facade_image):
        pass
    faults = {}
    try:
        validate_fixture(None)
    except ValueError:
        faults["missing_fixture"] = "rejected"
    observed = deepcopy(readback("before"))
    if not isinstance(observed, dict) or not isinstance(observed.get("gateway"), dict):
        raise FixtureUnavailable("independent gateway runtime cannot be inspected")
    original = observed["gateway"].get("image")
    observed["gateway"]["image"] = "sha256:" + ("0" if original != "sha256:" + "0" * 64 else "1") * 64
    try:
        with client_runtime_window(manifest, lambda phase: observed, source=source, account=account,
                                   gateway_image=gateway_image, facade_image=facade_image):
            raise ValueError("changed digest was not rejected")
    except ValueError as exc:
        if str(exc) == "changed digest was not rejected":
            raise
        faults["changed_digest"] = "rejected"
    try:
        run_bounded([sys.executable, "-c", "import time; time.sleep(0.5)"], environ={},
                    timeout=0.01, max_input_bytes=0, max_output_bytes=64)
    except ProcessLimitError:
        faults["timeout"] = "rejected"
    try:
        confirmed_cleanup({"cleanup": {"status": "pending"}, "provider_observation": {}})
    except ValueError:
        faults["unconfirmed_cleanup"] = "rejected"
    if set(faults) != {"missing_fixture", "changed_digest", "timeout", "unconfirmed_cleanup"}:
        raise ValueError("bounded offline fault probes did not reject every unsafe outcome")
    return {"source": "isolated-bounded-faults", "faults": faults}


def evaluate(pattern, output, gateway_url, gateway_revision, gateway_image, facade_image, *, command,
             scenarios=SCENARIOS, client_factory=MCPClient, fault_driver_factory=None,
             provider_observer=None, access_driver_factory=None, access_observer=None,
             integration_driver_factory=None, integration_observer=None,
             cleanup_driver_factory=None, cleanup_observer=None, live=False,
             contract_fixture=None, pinned_client_factory=None, release_manifest=None, release_observer=None,
             release_source_revision=None, release_account=None, client_fixture=None,
             client_refresh_file=None, cell_filter=None, recovery_root=None, governance_fixture=None,
             governance_observer=None, atlassian_setup_fixture=None,
             atlassian_setup_driver_factory=None, atlassian_setup_observer=None,
             atlassian_operation_ledger=None, atlassian_provider_reader=None,
             atlassian_client_fixtures=None, atlassian_native_plans=None, atlassian_recovery_handoff=None,
             atlassian_regression_fixtures=None, atlassian_flow_binding=None, atlassian_authority_campaign=None,
             atlassian_release_campaign=None, atlassian_context_factory=None):
    if not IMAGE.fullmatch(facade_image) or not IMAGE.fullmatch(gateway_image) or not REVISION.fullmatch(gateway_revision):
        raise ValueError("verified gateway and façade images and expected revision required")
    report = {"evidence_schema": "workflow-evaluation/v1", "schema_version": 1,
              "live_attestation": live, "scenario_filter": pattern,
              "github_run_id": os.environ.get("GITHUB_RUN_ID"),
              "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
              "deployed_revisions": {"gateway": gateway_revision, "gateway_image": gateway_image, "facade_image": facade_image},
              "started_at": datetime.now(UTC).isoformat(), "scenarios": [],
              "harness_revision": os.environ.get("GITHUB_SHA"),
              "matrix_complete": cell_filter is None}
    atlassian_native = None
    try:
        validate_scenarios(scenarios)
        try:
            selected = select_scenarios(pattern, scenarios)
            report["selected_scenario_ids"] = [case.identifier for case in selected]
            required_atlassian = {f"SCN-8454-{number:02}" for number in range(1, 7)
                                  if pattern == "repair" or fnmatch.fnmatchcase(f"SCN-8454-{number:02}", pattern)}
            report["unregistered_scenarios"] = sorted(required_atlassian - {case.identifier for case in scenarios})
            if report["unregistered_scenarios"]:
                report["matrix_complete"] = False
            if cell_filter is not None:
                report["live_attestation"] = False
            if live and any(case.action in {"release_ac01", "release_ac02", "release_ac03"} for case in selected):
                report["live_attestation"] = False
            if live and any(case.action == "access_live" for case in selected) and len(selected) != 1:
                raise ValueError("SCN-8065-04 must run alone to preserve the $20 test-run bound; "
                                 "qualification must enumerate every repair case and run it separately")
            if live and any(case.action == "oauth_ac04" for case in selected) and len(selected) != 1:
                raise ValueError("SCN-8074-04 must run alone to preserve the $20 test-run bound; "
                                 "qualification must enumerate every repair case and run it separately")
            if live and any(case.action == "github_ac04" for case in selected) and len(selected) != 1:
                raise ValueError("SCN-8075-04 must run alone to preserve the $20 test-run bound; "
                                 "qualification must enumerate every repair case and run it separately")
            if live and any(case.action == "jira_ac04" for case in selected) and len(selected) != 1:
                raise ValueError("SCN-8076-04 must run alone to preserve the $20 test-run bound; "
                                 "qualification must enumerate every repair case in the staged AC-04 workflow")
            if live and any(case.action == "slack_ac04" for case in selected) and len(selected) != 1:
                raise ValueError("SCN-8077-04 must run alone to preserve the $20 test-run bound; "
                                 "qualification must enumerate every repair case and run it separately")
        except ValueError as exc:
            report["selection_error"] = str(exc)
            raise
        if cell_filter is not None and not any(case.action == "client_matrix" for case in selected):
            raise ValueError("cell filter requires the installed-client matrix")
        if atlassian_client_fixtures is not None and any(case.action in {"atlassian_client_matrix", "atlassian_recovery"} for case in selected):
            validate_isolated_fixtures(atlassian_client_fixtures)
            if live and atlassian_native_plans is not None:
                from tests.e2e.tools.atlassian_native_matrix import NativeMatrix

                atlassian_native = NativeMatrix(atlassian_native_plans, atlassian_client_fixtures,
                    gateway_url=gateway_url, gateway_revision=gateway_revision, client_factory=client_factory,
                    lose_last_write_receipt=any(case.action == "atlassian_recovery" for case in selected))
        selected_cells = [(case, client_name, tool) for case in selected for client_name, tool in
                          ([(client_name, "install") for client_name in CLIENTS] if case.action == "client_install"
                           else [(client_name, operation) for client_name in CLIENTS for operation in OPERATIONS
                                 if cell_filter is None or cell_filter == client_name + ":" + operation]
                           if case.action == "client_matrix"
                           else [(client_name, "faults") for client_name in CLIENTS]
                           if case.action == "client_faults"
                           else [(client_name, "evidence") for client_name in CLIENTS]
                           if case.action == "client_evidence"
                           else [(client_name, "search") for client_name in CLIENTS]
                           if case.action in {"search_ac04", "interpreter_ac04"}
                           else [(client_name, "contract") for client_name in CLIENTS] if case.action == "contract_ac04"
                           else [("cli", "contract")] if case.action in {"contract_ac01", "contract_ac02", "contract_ac03", "search_ac01", "search_ac02", "interpreter_ac01", "interpreter_ac02", "interpreter_ac03"}
                           else [("cli", tool) for tool in ("search", "interpreter", "browser")]
                                if case.action in {"cleanup_ac01", "cleanup_ac02"}
                           else [("cli", "search")]
                                if case.action in {"cleanup_ac03", "cleanup_ac04", "search_ac03"}
                           else [(client_name, "browser") for client_name in CLIENTS] if case.action == "browser_ac04"
                           else [("cli", "browser")] if case.action in {"browser_ac01", "browser_ac02", "browser_ac03"}
                           else [("cli", operation) for operation in KNOWLEDGE_OPERATIONS] if case.action == "knowledge_ac01"
                           else [(client_name, operation) for client_name in CLIENTS for operation in KNOWLEDGE_OPERATIONS] +
                                [("engine-worker", "acl")] if case.action == "knowledge_ac04"
                           else [("cli", "search")] if case.action in {"knowledge_ac02", "knowledge_ac03"}
                           else [(client_name, "broker") for client_name in CLIENTS] if case.action == "broker_ac04"
                           else [("cli", "broker")] if case.action in {"broker_ac01", "broker_ac02", "broker_ac03"}
                           else [("cli", family) for family in GOVERNANCE_FAMILIES] if case.action == "governance_ac01"
                           else [("cli", "replay")] if case.action == "governance_ac02"
                           else [("cli", "activity")] if case.action == "governance_ac03"
                           else [(client_name, tool) for client_name in CLIENTS for tool in ("search", "interpreter", "browser")]
                                if case.action == "governance_ac04"
                           else [("cli", "github")] if case.action in {"oauth_ac01", "oauth_ac02", "oauth_ac03", "github_ac01", "github_ac02", "github_ac03"}
                           else [("cli", "jira")] if case.action in {"jira_ac01", "jira_ac02", "jira_ac03"}
                           else [(client_name, "jira") for client_name in CLIENTS] if case.action == "jira_ac04"
                           else [("cli", "slack")] if case.action in {"slack_ac01", "slack_ac02", "slack_ac03"}
                           else [(client_name, "slack") for client_name in CLIENTS] if case.action == "slack_ac04"
                           else [(client_name, "github") for client_name in CLIENTS] if case.action in {"oauth_ac04", "github_ac04"}
                           else [("cli", "gateway")] if case.action == "access_live"
                           else [("operator", "setup")] if case.action == "atlassian_setup"
                           else [(client_name, tool) for client_name in sorted(ATLASSIAN_CLIENTS)
                                 for tool in ATLASSIAN_OPERATION_ORDER]
                                if case.action == "atlassian_client_matrix"
                           else [(client_name, "recovery") for client_name in sorted(ATLASSIAN_CLIENTS)]
                                if case.action == "atlassian_recovery"
                           else [("operator", "regression")] if case.action == "atlassian_regression"
                           else [("operator", "authority")] if case.action == "atlassian_authority"
                           else [("operator", "handoff")] if case.action == "atlassian_handoff"
                           else [("operator", "release")] if case.action in {"release_ac01", "release_ac02", "release_ac03", "release_ac04"}
                           else matrix_cells(case.identifier))]
        for case, client_name, tool in selected_cells:
            report["scenarios"].append({"scenario_id": case.identifier,
                                        "cell_id": client_name + ":" + tool,
                                        "tags": ["@scenario " + case.identifier] +
                                        [("@acceptance " if story.startswith("AC-") else "@story ") + story
                                         for story in case.stories],
                                        "client": client_name, "tool": tool,
                                        "client_version": None, "status": "not-run"})
            if match := REPAIR_SCENARIO.fullmatch(case.identifier):
                row = report["scenarios"][-1]
                row.update({"issue_number": int(match[1]), "local_scenario_id": "SCN-" + match[2]})
                row["tags"].insert(1, "@story " + match[1])
        release_only = all(case.action in {"release_ac01", "release_ac02", "release_ac03", "release_ac04", "atlassian_setup"} for case in selected)
        governance_only = all(case.action.startswith("governance_ac") for case in selected)
        client_only = all(case.action in {"client_install", "client_matrix", "atlassian_setup", "atlassian_client_matrix", "atlassian_recovery", "atlassian_regression", "atlassian_authority", "atlassian_handoff", "client_faults", "client_evidence",
                                          "search_ac04", "interpreter_ac04", "governance_ac01",
                                          "governance_ac02", "governance_ac03", "governance_ac04"} for case in selected)
        if (not release_only and not governance_only) or (live and any(case.action == "atlassian_setup" for case in selected)):
            verify_gateway(gateway_url)
        reserved_cost_usd = 0
        estimated_all_in = Decimal("0")
        with nullcontext(None) if release_only or client_only else client_factory(command) as client:
            for (case, client_name, tool), row in zip(selected_cells, report["scenarios"]):
                if client is not None and client_name == "cli" and case.action != "contract_ac04":
                    row["client_version"] = client.version
                try:
                    if case.action == "atlassian_handoff":
                        row.update(status="blocked", evidence_scope="deployed-target" if live else "repository-integration",
                                   cleanup={"status": "pending"}, reason="Protected release observations and verified client/provider joins required")
                        if atlassian_release_campaign is not None:
                            row["observations"] = atlassian_release_campaign.collect()
                        continue
                    if case.action == "atlassian_authority":
                        from tests.e2e.tools.atlassian_authority_scenario import collect_authority, qualify_authority

                        row.update(status="blocked", evidence_scope="deployed-target" if live else "repository-integration",
                                   cleanup={"status": "pending"}, reason="Protected native authority campaign required")
                        if atlassian_authority_campaign is None:
                            continue
                        evidence = collect_authority(atlassian_authority_campaign)
                        row["observations"] = evidence
                        row["authority"] = qualify_authority(evidence, manifest=atlassian_authority_campaign.config)
                        if row["authority"]["status"] == "pass":
                            row.update(status="pass", cleanup={"status": "confirmed"}, reason="All original authority cases verified")
                        else:
                            row["reason"] = "Authority campaign retains unverified observations"
                        continue
                    if case.action == "atlassian_regression":
                        from tests.e2e.tools.atlassian_regression_scenario import collect_regression, predecessor_gaps, qualify_regression

                        row.update(status="blocked", evidence_scope="deployed-target" if live else "repository-integration",
                                   cleanup={"status": "pending"}, reason="Protected legacy drivers and original release binding required")
                        if atlassian_flow_binding is None:
                            continue
                        evidence = collect_regression(binding=atlassian_flow_binding, target=report["deployed_revisions"],
                                                      fixtures=atlassian_regression_fixtures, live=live)
                        row["observations"] = evidence
                        row["predecessors"] = predecessor_gaps(evidence, binding=atlassian_flow_binding,
                                                               target=report["deployed_revisions"], require_live=live)
                        try:
                            row["regression"] = qualify_regression(evidence, binding=atlassian_flow_binding,
                                                                  target=report["deployed_revisions"], require_live=live)
                        except ValueError:
                            continue
                        row.update(status="pass", cleanup={"status": "confirmed"},
                                   reason="Original legacy client matrix verified; predecessor gaps remain explicit")
                        continue
                    if case.action == "atlassian_recovery":
                        from tests.e2e.tools.atlassian_recovery_scenario import run_recovery

                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        if atlassian_native is None:
                            row.update(status="blocked", reason="Original interrupted native fixtures are required")
                        else:
                            row.update(run_recovery(atlassian_native, client_name, handoff=atlassian_recovery_handoff))
                            if row["status"] == "pass":
                                for previous in report["scenarios"]:
                                    if previous["scenario_id"] == "SCN-8454-02" and previous["client"] == client_name:
                                        previous.update(atlassian_native.execute(client_name, previous["tool"]))
                                if row["recovery"]["runner_loss"] != "persisted-runner-loss":
                                    row.update(status="blocked", reason="Worker recovery verified; durable publication and fresh-root recovery are unverified")
                        continue
                    if case.action == "atlassian_client_matrix":
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        if not live:
                            report["live_attestation"] = False
                        fixture = (atlassian_client_fixtures or {}).get(client_name)
                        operation_ledger = fixture["ledger"] if fixture is not None else atlassian_operation_ledger
                        provider_reader = fixture["reader"] if fixture is not None else atlassian_provider_reader
                        if live:
                            if atlassian_native is None:
                                row["status"], row["reason"] = "blocked", "original native approvals and isolated client plans unavailable"
                            else:
                                row.update(atlassian_native.execute(client_name, tool))
                        elif operation_ledger is None or provider_reader is None:
                            row["status"] = "blocked"
                            row["reason"] = "private operation ledger or independent provider reader unavailable"
                        else:
                            try:
                                row["observations"] = observe_atlassian_cell(
                                    operation_ledger, provider_reader,
                                    client=client_name, tool=tool)
                            except ValueError:
                                row["status"] = "blocked"
                                row["reason"] = "confirmed client operation or independent provider readback unavailable"
                            else:
                                row["status"] = "pass"
                                row["reason"] = "controlled operation and direct provider readback match"
                        continue
                    if case.action == "atlassian_setup":
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        row["cleanup"] = {"status": "not-started", "provider_termination": "not-started"}
                        if not isinstance(atlassian_setup_fixture, dict) or set(atlassian_setup_fixture) != {"ledger", "policy_required"}:
                            row["status"], row["reason"] = "blocked", "private setup fixture and policy role unavailable"
                            continue
                        if live:
                            driver = atlassian_setup_driver_factory(case) if atlassian_setup_driver_factory else None
                            if driver is None or atlassian_setup_observer is None:
                                row["status"], row["reason"] = "blocked", "protected setup driver and independent observer unavailable"
                                continue
                            row["observations"] = run_live_connect(
                                **atlassian_setup_fixture, driver=driver, observer=atlassian_setup_observer)
                            row["cleanup"] = {"status": row["observations"]["cleanup"]}
                        else:
                            report["live_attestation"] = False
                            row["observations"] = live_connect(**atlassian_setup_fixture)
                        row["status"] = row["observations"]["status"]
                        row["reason"] = ("independent setup and cleanup verified" if live else "controlled setup actions observed") if row["status"] == "pass" else "setup actions or cleanup remain unverified"
                        continue
                    if case.action.startswith("governance_ac"):
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        report["live_attestation"] = False
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "approved all-in envelope and independent provider/export/cleanup readback unavailable"
                            continue
                        observation = (governance_fixture or {}).get(case.action, {}).get(
                            client_name + ":" + tool if case.action == "governance_ac04" else tool)
                        if observation is None:
                            row["status"] = "blocked"
                            row["reason"] = "isolated governance observation unavailable"
                            continue
                        row["observations"] = (governance_ac01(tool, observation)
                                               if case.action == "governance_ac01"
                                               else governance_ac02(observation) if case.action == "governance_ac02"
                                               else governance_ac03(observation) if case.action == "governance_ac03"
                                               else governance_ac04_link(client_name, tool, observation, governance_observer))
                        row["status"] = "pass"
                        continue
                    if case.action in {"release_ac01", "release_ac02", "release_ac03"}:
                        row["evidence_scope"] = "repository-integration"
                        row["cleanup"] = {"status": "not-required", "provider_termination": "not-started"}
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "isolated release fixture cannot establish deployed acceptance; run it without live mode"
                            continue
                        handler = {"release_ac01": release_ac01, "release_ac02": release_ac02,
                                   "release_ac03": release_ac03}[case.action]
                        row["status"], row["reason"], row["observations"] = handler(contract_fixture)
                        continue
                    if case.action == "release_ac04":
                        row["cleanup"] = {"status": "not-required", "provider_termination": "not-started"}
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        selected_manifest = release_manifest if live else contract_fixture
                        row["status"], row["reason"], row["observations"] = release_ac04(
                            selected_manifest, readback=release_observer if live else None,
                            source=release_source_revision or gateway_revision,
                            account=release_account or (selected_manifest or {}).get("account", ""),
                            gateway_image=gateway_image, facade_image=facade_image, live=live)
                        if live:
                            if row["status"] != "pass":
                                report["live_attestation"] = False
                            else:
                                report["release_source_revision"] = release_manifest["source_revision"]
                                report["account"] = row["observations"]["account"]
                                report["environment"] = "dev"
                                report["github_run_id"] = int(os.environ.get("GITHUB_RUN_ID", "0"))
                                report["github_run_attempt"] = int(os.environ.get("GITHUB_RUN_ATTEMPT", "0"))
                        continue
                    if case.action in {"access_ac01", "access_ac02", "access_ac03"}:
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "local integration scenario cannot establish deployed acceptance; run it without live mode"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            selector = {"access_ac01": access_ac01, "access_ac02": access_ac02,
                                        "access_ac03": access_ac03}[case.action]
                            row.update(selector(driver, integration_observer))
                        except FixtureUnavailable:
                            row["reason"] = "isolated access fixture and independent observer unavailable"
                        continue
                    if case.action in {"knowledge_ac01", "knowledge_ac02", "knowledge_ac03", "knowledge_ac04"}:
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "deployed-target" if live and case.action == "knowledge_ac04" else "repository-integration"
                        if case.action == "knowledge_ac04":
                            row["fixture"] = {"scope": "run-owned-isolated", "max_sessions": 1,
                                              "ttl_seconds": 300, "cleanup_seconds": 600,
                                              "max_cost_usd": "20"}
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "authorized private client and indexed worker bindings unavailable" if case.action == "knowledge_ac04" else "local knowledge fixture cannot establish deployed acceptance"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            result = (knowledge_ac01(driver, integration_observer, tool) if case.action == "knowledge_ac01"
                                      else knowledge_ac02(driver, integration_observer) if case.action == "knowledge_ac02"
                                      else knowledge_ac03(driver, integration_observer) if case.action == "knowledge_ac03"
                                      else knowledge_ac04(driver, integration_observer, client_name, tool))
                            row.update(result)
                        except FixtureUnavailable as exc:
                            row["reason"] = str(exc)
                        continue
                    if case.action in {"broker_ac01", "broker_ac02", "broker_ac03", "broker_ac04"}:
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "deployed-target" if live and case.action == "broker_ac04" else "repository-integration"
                        if live and case.action != "broker_ac04":
                            row["status"] = "blocked"
                            row["reason"] = "local broker fixture cannot establish deployed acceptance"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            if live and (driver is None or integration_observer is None or
                                         getattr(driver, "deployed_target", False) is not True or
                                         getattr(integration_observer, "deployed_target", False) is not True):
                                row["status"] = "blocked"
                                row["reason"] = "protected broker deployment, owner consent and independent provider observer required"
                                continue
                            if case.action == "broker_ac04":
                                row.update(broker_ac04(driver, integration_observer, client_name))
                            else:
                                selector = {"broker_ac01": broker_ac01, "broker_ac02": broker_ac02, "broker_ac03": broker_ac03}[case.action]
                                row.update(selector(driver, integration_observer))
                        except FixtureUnavailable as exc:
                            row["status"] = "blocked"
                            row["reason"] = str(exc)
                        continue
                    if case.action in {"oauth_ac01", "oauth_ac02", "oauth_ac03"}:
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "local OAuth fixture cannot establish deployed acceptance"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            selector = {"oauth_ac01": oauth_ac01, "oauth_ac02": oauth_ac02, "oauth_ac03": oauth_ac03}[case.action]
                            row.update(selector(driver, integration_observer))
                        except FixtureUnavailable as exc:
                            row["status"] = "blocked"
                            row["reason"] = str(exc)
                        continue
                    if case.action in {"github_ac01", "github_ac02", "github_ac03", "jira_ac01", "jira_ac02", "jira_ac03", "slack_ac01", "slack_ac02", "slack_ac03"}:
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "local OAuth fixture cannot establish deployed acceptance"
                            continue
                        if case.action.startswith("jira_") and integration_observer is None:
                            row["status"] = "blocked"
                            row["reason"] = "independent Jira owner and provider observer required"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            selector = {"github_ac01": github_ac01, "github_ac02": github_ac02,
                                        "github_ac03": github_ac03, "jira_ac01": jira_ac01, "jira_ac02": jira_ac02, "jira_ac03": jira_ac03,
                                        "slack_ac01": slack_ac01, "slack_ac02": slack_ac02,
                                        "slack_ac03": slack_ac03}[case.action]
                            row.update(selector(driver, integration_observer))
                        except FixtureUnavailable as exc:
                            row["status"] = "blocked"
                            row["reason"] = str(exc)
                        continue
                    if case.action == "oauth_ac04":
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        try:
                            if live and (integration_observer is None or
                                         getattr(integration_observer, "deployed_target", False) is not True):
                                row["status"] = "blocked"
                                row["reason"] = "protected OAuth deployment, owner consent and independent provider observer required"
                                continue
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            if live and (driver is None or getattr(driver, "deployed_target", False) is not True):
                                row["status"] = "blocked"
                                row["reason"] = "protected OAuth deployment, owner consent and independent provider observer required"
                                continue
                            row.update(oauth_ac04(driver, integration_observer, client_name, {
                                "gateway": gateway_revision, "gateway_image": gateway_image,
                                "facade_image": facade_image,
                            }))
                        except FixtureUnavailable as exc:
                            row["status"] = "blocked"
                            row["reason"] = str(exc)
                        continue
                    if case.action == "github_ac04":
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        try:
                            if live and (integration_observer is None or
                                         getattr(integration_observer, "deployed_target", False) is not True):
                                row["status"] = "blocked"
                                row["reason"] = "protected GitHub deployment, OAuth consent and independent provider observer required"
                                continue
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            if live and (driver is None or getattr(driver, "deployed_target", False) is not True):
                                row["status"] = "blocked"
                                row["reason"] = "protected GitHub deployment, OAuth consent and independent provider observer required"
                                continue
                            row.update(github_ac04(driver, integration_observer, client_name, {
                                "gateway": gateway_revision, "gateway_image": gateway_image,
                                "facade_image": facade_image,
                            }))
                        except FixtureUnavailable as exc:
                            row["status"] = "blocked"
                            row["reason"] = str(exc)
                        continue
                    if case.action in {"jira_ac04", "slack_ac04"}:
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        try:
                            if live and (integration_observer is None or
                                         getattr(integration_observer, "deployed_target", False) is not True):
                                row["status"] = "blocked"
                                row["reason"] = "protected deployment, explicit owner consent and independent provider observer required"
                                continue
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            if live and (driver is None or getattr(driver, "deployed_target", False) is not True):
                                row["status"] = "blocked"
                                row["reason"] = "protected deployment, explicit owner consent and independent provider observer required"
                                continue
                            checker = jira_ac04 if case.action == "jira_ac04" else slack_ac04
                            row.update(checker(driver, integration_observer, client_name, {
                                "gateway": gateway_revision, "gateway_image": gateway_image,
                                "facade_image": facade_image,
                            }))
                        except FixtureUnavailable as exc:
                            row["status"] = "blocked"
                            row["reason"] = str(exc)
                        continue
                    if case.action == "browser_ac01":
                        row["cleanup"] = {"status": "not-run", "provider_termination": None}
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "local browser fixture cannot establish deployed acceptance"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            row.update(browser_ac01(driver, integration_observer, gateway_revision))
                        except FixtureUnavailable:
                            row["reason"] = "isolated inert HTTPS browser fixture and independent observer unavailable"
                        continue
                    if case.action in {"browser_ac02", "browser_ac03"}:
                        row["cleanup"] = {"status": "not-run", "provider_termination": None}
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "isolated browser fixture cannot establish deployed acceptance"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            selector = browser_ac02 if case.action == "browser_ac02" else browser_ac03
                            row.update(selector(driver, integration_observer))
                        except FixtureUnavailable:
                            row["reason"] = "isolated browser fixture and independent observer unavailable"
                        continue
                    if case.action == "browser_ac04":
                        row["cleanup"] = {"status": "not-run", "provider_termination": None}
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        if live:
                            if (release_manifest is None or not callable(release_observer) or
                                    not release_source_revision or not release_account or
                                    not client_refresh_file or not recovery_root or
                                    integration_driver_factory is None or integration_observer is None or
                                    (client_fixture or {}).get("browser_lifecycle") is None):
                                row["status"] = "blocked"
                                row["reason"] = "protected release, private fixture and independent observer unavailable"
                                continue
                            try:
                                try:
                                    reservation = validate_fixture(client_fixture["browser_lifecycle"], client_name=client_name)
                                except ValueError:
                                    row["status"] = "blocked"
                                    row["reason"] = "priced authorized browser lifecycle fixture unavailable"
                                    continue
                                if estimated_all_in + reservation["reserved_usd"] > Decimal("20"):
                                    row["status"] = "blocked"
                                    row["reason"] = "run-wide $20 cost bound exceeded"
                                    continue
                                require_paid_admission()
                                estimated_all_in += reservation["reserved_usd"]
                                with client_runtime_window(release_manifest, release_observer,
                                        source=release_source_revision, account=release_account,
                                        gateway_image=gateway_image, facade_image=facade_image) as window:
                                    driver = integration_driver_factory(case, client_name, tool)
                                    row.update(browser_ac04(driver, integration_observer, client_name, gateway_revision))
                                    row["runtime_window"] = window
                            except FixtureUnavailable:
                                row["status"] = "blocked"
                                row["reason"] = "authoritative operation-keyed admission unavailable"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            row.update(browser_ac04(driver, integration_observer, client_name, gateway_revision))
                        except FixtureUnavailable:
                            row["reason"] = "isolated installed-client browser fixture and independent observer unavailable"
                        continue
                    if case.action in {"cleanup_ac01", "cleanup_ac02", "cleanup_ac03", "cleanup_ac04"}:
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "isolated cleanup fixture cannot establish deployed acceptance"
                            continue
                        try:
                            driver = cleanup_driver_factory(case, client_name, tool) if cleanup_driver_factory else None
                            selector = {"cleanup_ac01": cleanup_ac01, "cleanup_ac02": cleanup_ac02,
                                        "cleanup_ac03": cleanup_ac03, "cleanup_ac04": cleanup_ac04}[case.action]
                            row.update(selector(driver, cleanup_observer, tool))
                        except FixtureUnavailable:
                            row["reason"] = "isolated cleanup fixture and independent observer unavailable"
                        continue
                    if case.action == "search_ac03":
                        row["cleanup"] = {"status": "not-run"}
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "isolated five-helper fixture cannot establish deployed client acceptance"
                            continue
                        try:
                            driver = cleanup_driver_factory(case, client_name, tool) if cleanup_driver_factory else None
                            row.update(cleanup_ac04(driver, cleanup_observer, tool))
                        except FixtureUnavailable:
                            row["reason"] = "isolated five-helper fixture and independent observer unavailable"
                        continue
                    if case.action == "access_live":
                        row["fixture"] = {"scope": "run-owned-isolated", "max_sessions": 1,
                                          "ttl_seconds": 300, "cleanup_seconds": 600, "max_cost_usd": "20"}
                        row["cleanup"] = {"status": "not-run", "provider_termination": None}
                        if not live or client_name != "cli":
                            row["reason"] = "protected selected-tenant fixture unavailable"
                            continue
                        try:
                            driver = access_driver_factory(case, client_name, tool) if access_driver_factory else None
                            row.update(run_access_fixture(ACCESS_LIVE_FIXTURE, driver))
                            row["status"], row["reason"] = access_verdict(
                                ACCESS_LIVE_FIXTURE, row, observer=access_observer, live=live,
                            )
                        except FixtureUnavailable:
                            row["reason"] = "approved private fixture and independent observer unavailable"
                        continue
                    if case.action == "client_install":
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        if not live or not client_refresh_file:
                            row["reason"] = "protected installed session or gateway release unavailable"
                            continue
                        if (release_manifest is None or not callable(release_observer) or
                                not release_source_revision or not release_account):
                            row["status"] = "blocked"
                            row["reason"] = "approved release receipts or independent runtime readback unavailable"
                            continue
                        with client_runtime_window(release_manifest, release_observer,
                                                   source=release_source_revision, account=release_account,
                                                   gateway_image=gateway_image, facade_image=facade_image) as window:
                            observation = harness_ac01(client_name, gateway_url=gateway_url,
                                                       gateway_revision=gateway_revision,
                                                       refresh_file=client_refresh_file)
                        observation["runtime_window"] = window
                        row["observations"] = observation
                        row["client_version"] = observation["client_version"]
                        row["status"], row["reason"] = "pass", "served CLI and client discovery verified"
                        continue
                    if case.action == "client_matrix":
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        if live:
                            report["live_attestation"] = False
                            row["status"] = "blocked"
                            row["reason"] = ("trusted all-in model and provider reservation and operation-keyed "
                                             "replay accounting (#8078) unavailable; paid client calls forbidden in CI")
                            continue
                        fixture = (client_fixture or {}).get("operations", {}).get(tool)
                        if fixture is None or provider_observer is None or not client_refresh_file or not recovery_root:
                            row["reason"] = "priced authorized fixture, private recovery home, installed session or independent observer unavailable"
                            continue
                        reservation = validate_fixture(fixture, client_name=client_name)
                        if estimated_all_in + reservation["reserved_usd"] > Decimal("20"):
                            row["reason"] = "run-wide $20 cost bound exceeded"
                            continue
                        estimated_all_in += reservation["reserved_usd"]
                        try:
                            require_paid_admission()
                        except FixtureUnavailable:
                            row["status"] = "blocked"
                            row["reason"] = "authoritative #8078 operation-keyed admission unavailable"
                            row["cleanup"] = {"status": "unconfirmed", "provider_termination": None}
                            continue
                        row["recovery_id"] = str(uuid4())
                        observation = harness_ac02(client_name, tool, fixture, gateway_url=gateway_url,
                                                   gateway_revision=gateway_revision,
                                                   refresh_file=client_refresh_file, observer=provider_observer,
                                                   recovery_root=recovery_root, recovery_id=row["recovery_id"],
                                                   models=(client_fixture or {}).get("client_models"))
                        confirmed_cleanup(observation)
                        row["observations"] = observation
                        row["client_version"] = observation["client_version"]
                        row["status"] = "pass" if live else "blocked"
                        row["cleanup"] = observation["cleanup"]
                        row["reason"] = "independent protected readback confirmed" if live else "local fixture cannot establish deployed acceptance"
                        continue
                    if case.action == "client_faults":
                        row["evidence_scope"] = "repository-integration"
                        report["live_attestation"] = False
                        try:
                            row["observations"] = harness_ac03(
                                manifest=release_manifest, readback=release_observer,
                                source=release_source_revision, account=release_account,
                                gateway_image=gateway_image, facade_image=facade_image)
                        except FixtureUnavailable:
                            row["reason"] = "approved runtime receipt or independent observer unavailable"
                            continue
                        row["status"] = "blocked"
                        row["cleanup"] = {"status": "unconfirmed", "provider_termination": None}
                        row["reason"] = "isolated fault probes cannot establish real installed-client invocation or cleanup"
                        continue
                    if case.action == "client_evidence":
                        row["reason"] = "required installed-client fixture or protected evidence unavailable"
                        continue
                    if case.action == "interpreter_ac04":
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        row["cleanup"] = {"status": "not-run"}
                        matrix = (client_fixture or {}).get("interpreter_matrix", {})
                        fixture = matrix.get(client_name)
                        if (fixture is None or provider_observer is None or pinned_client_factory is None or
                                not all(callable(getattr(provider_observer, name, None)) for name in
                                            ("observe", "session_status", "provider_calls")) or
                                any(not isinstance(matrix.get(name), dict) or
                                    matrix[name].get("positive") != fixture.get("positive") for name in CLIENTS)):
                            row["reason"] = "matched priced client fixtures, pinned adapters and independent provider readback unavailable"
                            continue
                        try:
                            reservation = validate_interpreter_ac04_fixture(client_name, fixture)
                        except (FixtureUnavailable, ValueError):
                            row["reason"] = "fresh priced and authorized interpreter fixture unavailable"
                            continue
                        if estimated_all_in + reservation["reserved_usd"] > Decimal("20"):
                            row["status"] = "blocked"
                            row["reason"] = "run-wide $20 cost bound exceeded"
                            continue
                        estimated_all_in += reservation["reserved_usd"]
                        adapter = pinned_client_factory(client_name, fixture)
                        row["observations"] = interpreter_ac04(
                            client_name, fixture, adapter, provider_observer)
                        row["client_version"] = row["observations"]["client_version"]
                        row["cleanup"] = row["observations"]["cleanup"]
                        row["status"], row["reason"] = "pass", "client bytes, result and provider termination verified"
                        continue
                    if case.action == "search_ac04":
                        row["evidence_scope"] = "deployed-target" if live else "repository-integration"
                        row["cleanup"] = {"status": "not-run"}
                        if live:
                            report["live_attestation"] = False
                            row["status"] = "blocked"
                            row["reason"] = "operation-keyed paid admission and dedicated protected client fixtures unavailable"
                            continue
                        matrix = (client_fixture or {}).get("search_matrix", {})
                        fixture = matrix.get(client_name)
                        if fixture is None or provider_observer is None or pinned_client_factory is None:
                            row["reason"] = "isolated priced client matrix and independent provider readback unavailable"
                            continue
                        if any(not isinstance(matrix.get(name), dict) or
                               matrix[name].get("positive") != fixture.get("positive") for name in CLIENTS):
                            raise ValueError("every client must run the same positive and negative search matrix")
                        reservation = validate_fixture(fixture.get("pricing"), client_name=client_name)
                        if estimated_all_in + reservation["reserved_usd"] > Decimal("20"):
                            row["status"] = "blocked"
                            row["reason"] = "run-wide $20 cost bound exceeded"
                            continue
                        estimated_all_in += reservation["reserved_usd"]
                        adapter = pinned_client_factory(client_name, fixture)
                        row["observations"] = search_ac04(client_name, fixture, adapter, provider_observer)
                        row["client_version"] = row["observations"]["client_version"]
                        row["cleanup"] = row["observations"]["cleanup"]
                        row["status"], row["reason"] = "pass", "isolated client search matrix and independent evidence verified"
                        continue
                    if case.action == "contract_ac04":
                        fixture = contract_fixture.get(client_name) if isinstance(contract_fixture, dict) else None
                        try:
                            validate_contract_ac04_fixture(fixture)
                        except FixtureUnavailable:
                            row["reason"] = "priced authorized per-client fixture unavailable"
                            continue
                        if reserved_cost_usd + fixture["max_cost_usd"] > 20:
                            row["reason"] = "run-wide $20 cost bound exceeded"
                            continue
                        row["fixture_bounds"] = {key: fixture[key] for key in
                                                 ("max_seconds", "session_ttl_seconds", "cleanup_seconds", "max_cost_usd")}
                        if pinned_client_factory is None or (live and provider_observer is None):
                            row["reason"] = "pinned client adapter or independent provider observer unavailable"
                            continue
                        try:
                            adapter = pinned_client_factory(client_name, fixture)
                        except FixtureUnavailable:
                            adapter = None
                        if adapter is None:
                            row["reason"] = "pinned client adapter unavailable"
                            continue
                        reserved_cost_usd += fixture["max_cost_usd"]
                        with adapter as pinned:
                            row["client_version"] = getattr(pinned, "version", None)
                            row["status"], row["reason"], row["observations"] = contract_ac04(
                                pinned, fixture, provider_observer, live=live)
                        continue
                    if case.action == "contract_ac01":
                        if contract_fixture is None:
                            row["reason"] = "isolated search and interpreter fixture unavailable"
                            continue
                        row["observations"] = contract_ac01(client, contract_fixture)
                        row["status"] = "blocked"
                        row["reason"] = "independent provider readback and cleanup unavailable"
                        continue
                    if case.action == "interpreter_ac01":
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "local interpreter fixture cannot establish deployed acceptance"
                            continue
                        try:
                            row["observations"] = interpreter_ac01(client, contract_fixture, integration_observer)
                        except FixtureUnavailable as exc:
                            row["reason"] = str(exc)
                            continue
                        row["status"], row["reason"] = "pass", "exact interpreter result, bytes and stop independently verified"
                        continue
                    if case.action == "interpreter_ac02":
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "local denial fixture cannot establish deployed acceptance"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            row["observations"] = interpreter_ac02(driver, integration_observer)
                        except FixtureUnavailable as exc:
                            row["reason"] = str(exc)
                            continue
                        row["status"], row["reason"] = "pass", "denials and failed execution independently verified"
                        continue
                    if case.action == "interpreter_ac03":
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "local recovery fixture cannot establish deployed acceptance"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            row["observations"] = interpreter_ac03(driver, integration_observer)
                        except FixtureUnavailable as exc:
                            row["reason"] = str(exc)
                            continue
                        row["status"], row["reason"] = "pass", "single execute, retained bytes and provider termination verified"
                        continue
                    if case.action == "search_ac01":
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "local search fixture cannot establish deployed client acceptance"
                            continue
                        try:
                            row["observations"] = search_ac01(client, contract_fixture, integration_observer)
                        except FixtureUnavailable as exc:
                            row["reason"] = str(exc)
                            continue
                        row["status"], row["reason"] = "pass", "bounded search and receipt independently verified"
                        continue
                    if case.action == "search_ac02":
                        row["evidence_scope"] = "repository-integration"
                        if live:
                            row["status"] = "blocked"
                            row["reason"] = "local search refusal fixture cannot establish deployed client acceptance"
                            continue
                        try:
                            driver = integration_driver_factory(case, client_name, tool) if integration_driver_factory else None
                            row["observations"] = search_ac02(driver, integration_observer)
                        except FixtureUnavailable as exc:
                            row["reason"] = str(exc)
                            continue
                        row["status"], row["reason"] = "pass", "refusal, retry and replay controls verified"
                        continue
                    if case.action == "contract_ac02":
                        if contract_fixture is None:
                            row["reason"] = "isolated foreign-handle fixture unavailable"
                            continue
                        row["observations"] = contract_ac02(client, contract_fixture)
                        row["status"] = "blocked"
                        row["reason"] = "independent provider dispatch counter unavailable"
                        continue
                    if case.action == "contract_ac03":
                        if contract_fixture is None or fault_driver_factory is None:
                            row["reason"] = "isolated lost-reply fixture or fault driver unavailable"
                            continue
                        try:
                            driver = fault_driver_factory(case, client_name, tool)
                        except FixtureUnavailable:
                            driver = None
                        if driver is None:
                            row["reason"] = "isolated lost-reply fault driver unavailable"
                            continue
                        row["observations"] = contract_ac03(client, client_factory, command, driver, contract_fixture)
                        row["status"] = "blocked"
                        row["reason"] = "independent provider dispatch readback unavailable"
                        continue
                    if case.action == "lifecycle":
                        fixture = fixture_for_cell(FIXTURES[case.identifier], tool)
                        row["fixture"] = fixture_summary(fixture)
                        row.update({"execution_id": None, "run_id": None, "request_ids": [],
                                    "operation_ids": [], "provider_request_observations": [],
                                    "artifact_sha256": [],
                                    "cleanup": {"status": "not-run", "provider_termination": None}})
                        if client_name != "cli" or tool == "gateway":
                            row["reason"] = "real client or workload fixture unavailable"
                            continue
                        try:
                            driver = fault_driver_factory(case, client_name, tool) if fault_driver_factory else None
                            row.update(run_fixture(fixture, driver))
                            row["status"], row["reason"] = lifecycle_verdict(
                                fixture, row, observer=provider_observer, live=live
                            )
                        except FixtureUnavailable:
                            row["reason"] = "isolated authorized fault driver and provider observer unavailable"
                        continue
                    if case.action != "list_tools" or client_name != "cli":
                        row["reason"] = "real client or workload fixture unavailable"
                        continue
                    listing = client.request("tools/list")
                    names = [tool["name"] for tool in listing["tools"]]
                    if not any(name.startswith("websearch___") for name in names):
                        raise ValueError("deployed gateway did not expose an entitled search tool")
                    row["status"] = "blocked" if case.identifier == "SCN-TLS-0017" else "pass"
                    if row["status"] == "blocked":
                        row["reason"] = "artifact upload and gate consumption not yet verified"
                    row["observations"] = {"tools_list_sha256": hashlib.sha256(
                        json.dumps(sorted(names), separators=(",", ":")).encode()).hexdigest()}
                except (ValueError, KeyError, TimeoutError, OSError) as exc:
                    if case.action == "client_matrix" and row.get("recovery_id"):
                        block_uncertain_matrix(row, recovery_root, exc)
                    else:
                        row["status"], row["reason"] = "fail", type(exc).__name__
    except (ValueError, KeyError, TimeoutError, OSError) as exc:
        report["error"] = type(exc).__name__
    finally:
        if atlassian_native is not None:
            try:
                for client_name in ATLASSIAN_CLIENTS:
                    cleanup = atlassian_native.finalize(client_name)
                    for row in report["scenarios"]:
                        if row["scenario_id"] == "SCN-8454-02" and row["client"] == client_name:
                            if row.get("cleanup", {}).get("execution") == "confirmed":
                                row["cleanup"] = cleanup
                            if row.get("cleanup", {}).get("status") != "confirmed":
                                report["cleanup_incomplete"] = True
                atlassian_native.close()
            except (ValueError, OSError, TimeoutError):
                report["cleanup_incomplete"] = True
        report["completed_at"] = datetime.now(UTC).isoformat()
        if atlassian_flow_binding is not None and atlassian_release_campaign is not None:
            from tests.e2e.tools.atlassian_flow_producer import finalize_handoff

            try:
                context = atlassian_context_factory() if atlassian_context_factory is not None else {}
                context.update(release_manifest=atlassian_release_campaign.manifest,
                               release_binding=atlassian_release_campaign.binding)
                published = {**report, "scenarios": [client_evidence_row(row, report) if row["scenario_id"] == "SCN-8454-02"
                                                     else row for row in report["scenarios"]]}
                finalize_handoff(published, binding=atlassian_flow_binding, context=context)
            except (KeyError, OSError, TypeError, ValueError):
                report["error"] = "Independent release qualification unavailable"
        if (report.get("error") or report.get("cleanup_incomplete") or not report["matrix_complete"] or
                any(row["status"] != "pass" for row in report["scenarios"])):
            report["live_attestation"] = False
        write_results(output, report)
    return report["matrix_complete"] and not report.get("error") and not report.get("cleanup_incomplete") and bool(report["scenarios"]) and all(
        row["status"] == "pass" for row in report["scenarios"])


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--lint", action="store_true")
    parser.add_argument("--scenario-filter", default="SCN-TLS-00*")
    parser.add_argument("--cell-filter")
    parser.add_argument("--output")
    parser.add_argument("--gateway-url")
    parser.add_argument("--gateway-revision")
    parser.add_argument("--gateway-image")
    parser.add_argument("--facade-image")
    args = parser.parse_args(argv)
    if args.lint:
        try:
            validate_scenarios(SCENARIOS)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    if args.cell_filter and (args.scenario_filter != "SCN-8067-02" or
                             args.cell_filter not in {client + ":" + operation for client in CLIENTS
                                                      for operation in OPERATIONS}):
        parser.error("cell filter must identify one SCN-8067-02 client:operation cell")
    if not all((args.output, args.gateway_url, args.gateway_revision, args.gateway_image, args.facade_image)):
        parser.error("output, gateway URL, revision and image digests are required for a live run")
    from tests.e2e.tools.atlassian_scenario_selection import selection_flags

    try:
        selected_flags = selection_flags(args.scenario_filter)
    except ValueError as error:
        parser.error(str(error))
    live = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and os.environ.get("GITHUB_REF") == "refs/heads/main"
    access_driver_factory, access_observer = private_access_bindings() if live else (None, None)
    if live and args.scenario_filter == "SCN-8076-04":
        connection_driver_factory, connection_observer = private_jira_bindings()
    else:
        connection_driver_factory, connection_observer = private_slack_bindings() if live and args.scenario_filter == "SCN-8077-04" else (None, None)
    release_manifest, release_observer = private_release_bindings() if live else (None, None)
    client_fixture = provider_observer = pinned_client_factory = None
    atlassian_fixture = atlassian_driver_factory = atlassian_observer = None
    atlassian_client_fixtures = atlassian_native_plans = None
    atlassian_handoff = None
    atlassian_authority_campaign = None
    atlassian_release_campaign = None
    atlassian_regression_fixtures = None
    atlassian_flow_binding = None
    if selected_flags["atlassian_clients"] and release_manifest is not None:
        from tests.e2e.tools import atlassian_flow_summary

        atlassian_flow_binding = {
            "source_revision": os.environ.get("SOURCE_REVISION"), "definition_revision": os.environ.get("GITHUB_SHA"),
            "run_id": os.environ.get("GITHUB_RUN_ID"), "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "release_sha256": hashlib.sha256(atlassian_flow_summary.canonical(release_manifest)).hexdigest(),
        }
    if live and selected_flags["atlassian_handoff"]:
        from tests.e2e.tools.atlassian_release_binding import ReleaseCampaign

        try:
            atlassian_release_campaign = ReleaseCampaign(os.environ)
        except (KeyError, OSError, TypeError, ValueError):
            pass
    if live and selected_flags["atlassian_regression"] and os.environ.get("ADP_TOOLS_ATLASSIAN_REGRESSION_FILE"):
        from tests.e2e.tools.atlassian_regression_binding import LegacyCampaign

        atlassian_regression_fixtures = LegacyCampaign(os.environ, client_factory=MCPClient)
    if live and selected_flags["atlassian_recovery"]:
        from tests.e2e.tools.atlassian_recovery_handoff import workflow_handoff

        atlassian_handoff = workflow_handoff
    if live and selected_flags["atlassian_authority"] and atlassian_flow_binding is not None:
        from tests.e2e.tools.atlassian_authority_scenario import AuthorityCampaign

        try:
            atlassian_authority_campaign = AuthorityCampaign(
                os.environ["ADP_TOOLS_ATLASSIAN_AUTHORITY_FILE"], binding=atlassian_flow_binding,
                gateway_url=args.gateway_url, account=os.environ["EXPECTED_ACCOUNT"], region=os.environ["AWS_REGION"],
                reader_role=os.environ["ADP_TOOLS_ATLASSIAN_ADAPTER_OBSERVER_ROLE_ARN"], client_factory=MCPClient,
            )
        except (KeyError, OSError, ValueError):
            pass
    if live and selected_flags["atlassian_native"]:
        try:
            atlassian_client_fixtures = load_isolated_fixtures(os.environ["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"])
            if os.environ.get("ADP_TOOLS_ATLASSIAN_NATIVE_PLAN_FILE"):
                from tests.e2e.tools.atlassian_native_matrix import load_native_plans

                atlassian_native_plans = load_native_plans(os.environ["ADP_TOOLS_ATLASSIAN_NATIVE_PLAN_FILE"])
        except (KeyError, OSError, ValueError):
            pass
    if live and selected_flags["atlassian_setup"]:
        fixture_factory, atlassian_driver_factory, atlassian_observer = private_atlassian_setup_bindings(args.gateway_url, args.gateway_revision)
        if fixture_factory is not None:
            atlassian_fixture = fixture_factory()
    if live and args.scenario_filter == "SCN-8070-04":
        client_fixture, provider_observer, pinned_client_factory = private_interpreter_bindings()
    atlassian_context_factory = None
    if selected_flags["atlassian_handoff"]:
        from tests.e2e.tools.atlassian_flow_producer import protected_context

        atlassian_context_factory = lambda: protected_context(os.environ)

    passed = evaluate(args.scenario_filter, args.output, args.gateway_url, args.gateway_revision,
                      args.gateway_image, args.facade_image, command=["modules/gateway/cli/adp", "mcp"],
                      access_driver_factory=access_driver_factory, access_observer=access_observer, live=live,
                      integration_driver_factory=connection_driver_factory, integration_observer=connection_observer,
                      release_manifest=release_manifest, release_observer=release_observer,
                      release_source_revision=os.environ.get("SOURCE_REVISION"),
                      release_account=os.environ.get("EXPECTED_ACCOUNT"), client_fixture=client_fixture,
                      provider_observer=provider_observer, pinned_client_factory=pinned_client_factory,
                      client_refresh_file=os.environ.get("ADP_TOOLS_REFRESH_FILE"), cell_filter=args.cell_filter,
                      recovery_root=os.environ.get("ADP_TOOLS_RECOVERY_ROOT"),
                      atlassian_setup_fixture=atlassian_fixture,
                      atlassian_setup_driver_factory=atlassian_driver_factory,
                      atlassian_setup_observer=atlassian_observer,
                      atlassian_client_fixtures=atlassian_client_fixtures,
                      atlassian_native_plans=atlassian_native_plans,
                      atlassian_recovery_handoff=atlassian_handoff,
                      atlassian_authority_campaign=atlassian_authority_campaign,
                      atlassian_release_campaign=atlassian_release_campaign,
                      atlassian_regression_fixtures=atlassian_regression_fixtures,
                      atlassian_context_factory=atlassian_context_factory,
                      atlassian_flow_binding=atlassian_flow_binding)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
