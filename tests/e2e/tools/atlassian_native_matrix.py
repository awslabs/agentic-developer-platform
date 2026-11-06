"""Execute the two-client matrix through original native admission and private ledgers."""

import json
import os
import subprocess
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from modules.tools.platform.scripts.native_test_session import (
    ApprovedReference,
    NativeSession,
)
from tool_adapters.atlassian_observe import _observation_value

from tests.e2e.tools.atlassian_client_matrix import (
    CLIENTS,
    READS,
    WRITES,
    digest,
    observe_cell,
    valid_snapshot,
)
from tests.e2e.tools.atlassian_setup_live import private_file
from tests.e2e.tools.atlassian_flow_summary import _write_private
from tests.e2e.tools import client_matrix
from tests.e2e.tools.client_matrix import native_call, recovery_result
from tests.e2e.tools.installed_clients import (
    installed_clients,
    resume_installed_clients,
)
from tests.e2e.tools.native_client_transport import native_client_transport
from tests.e2e.tools.atlassian_fixture_cleanup import JiraFixtureLedger
from tests.e2e.tools.atlassian_fixture_provider import JiraFixtureProvider


CREATED_JIRA_ID = "adp-generated-jira-id"
JIRA_DEPENDENTS = {
    "get_jira_issue",
    "search_jira",
    "update_jira_issue",
    "comment_jira_issue",
    "transition_jira_issue",
}


def dependent_arguments(tool, arguments, provider_id):
    field = "jql" if tool == "search_jira" else "issueIdOrKey"
    placeholder = (
        'id = "' + CREATED_JIRA_ID + '"' if field == "jql" else CREATED_JIRA_ID
    )
    if (
        tool not in JIRA_DEPENDENTS
        or arguments.get(field) != placeholder
        or json.dumps(arguments).count(CREATED_JIRA_ID) != 1
    ):
        raise ValueError("Exact created Jira dependency required")
    return {**arguments, field: placeholder.replace(CREATED_JIRA_ID, provider_id)}


def transport_calls(plan):
    create_hash = digest(plan["cells"]["create_jira_issue"]["arguments"])
    result = []
    for tool, cell in plan["cells"].items():
        call = {
            "tool": "atlassian___" + tool,
            "arguments_sha256": digest(cell["arguments"]),
            "max_requests": 1,
        }
        if cell["expected"]["provider_id"] == CREATED_JIRA_ID:
            call["jira_dependency"] = {
                "create_arguments_sha256": create_hash,
                "arguments": cell["arguments"],
            }
        result.append(call)
    return result


def object_result(value):
    if type(value) is not dict:
        raise ValueError("Native provider result must be an object")
    return value


def provider_result(receipt):
    value = receipt.get("result")
    for _ in range(6):
        if type(value) is not dict or value.get("isError") is True or "error" in value:
            raise ValueError("Native provider result is unavailable")
        if "content" in value or "structuredContent" in value:
            return _observation_value(value, object_result)
        value = value.get("result")
    raise ValueError("Native provider result is unavailable")


def projected_arguments(arguments, paths):
    result = {}
    for name, path in paths.items():
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 256:
            raise ValueError("An exact argument projection is required")
        value = arguments
        for part in path[1:].split("/"):
            if type(value) is not dict or part not in value:
                raise ValueError("The selected argument projection is absent")
            value = value[part]
        result[name] = value
    return result


def client_result(receipt, cell, owner_id):
    expected = cell["expected"]
    if (
        receipt.get("status") != "confirmed"
        or receipt.get("result_status") != "success"
        or receipt.get("root_human_id") != owner_id
        or receipt.get("site_id", expected["site_id"]) != expected["site_id"]
    ):
        raise ValueError("The native receipt does not match its admitted owner or site")
    tool = expected["tool"]
    result = provider_result(receipt)
    resource_id = expected["provider_id"]
    if tool in WRITES:
        if tool == "create_jira_issue":
            resource_id = str(result.get("id", ""))
            if not resource_id:
                raise ValueError("The client did not return the created resource")
        projection = projected_arguments(cell["arguments"], cell["projection_paths"])
    else:
        if tool.startswith("search_"):
            items = result.get("issues" if tool == "search_jira" else "results")
            if not isinstance(items, list):
                raise ValueError("The client did not return search results")
            matches = [
                item
                for item in items
                if type(item) is dict and str(item.get("id")) == resource_id
            ]
            if len(matches) != 1:
                raise ValueError(
                    "The client search did not return exactly the sentinel"
                )
            result = matches[0]
        if str(result.get("id")) != resource_id:
            raise ValueError("The client returned another provider resource")
        if "jira" in tool:
            fields = result.get("fields", {})
            projection = {
                "summary": fields["summary"],
                "description": fields.get("description"),
            }
        else:
            projection = {
                "title": result["title"],
                "body": result["body"]["storage"]["value"],
            }
    return {
        "provider_id": resource_id,
        "site_id": expected["site_id"],
        "account_id": expected["account_id"],
        "content_sha256": digest(projection),
    }


def validate_plan(plan, fixture, client):
    if (
        type(plan) is not dict
        or set(plan) != {"approval_file", "login_file", "model", "bounds", "cells"}
        or type(plan["cells"]) is not dict
        or set(plan["cells"]) != READS | WRITES
        or not isinstance(plan["model"], str)
        or not plan["model"]
    ):
        raise ValueError("A complete protected native client plan is required")
    bounds = plan["bounds"]
    ceilings = {
        "max_model_requests": 4,
        "max_input_tokens": 65536,
        "max_output_tokens": 8192,
        "max_request_bytes": 65536,
        "max_response_bytes": 262144,
    }
    if (
        type(bounds) is not dict
        or set(bounds) != set(ceilings)
        or any(
            type(bounds[key]) is not int or not 0 < bounds[key] <= limit
            for key, limit in ceilings.items()
        )
    ):
        raise ValueError("Bounded original model invocation is required")
    identifiers, markers = set(), set()
    for tool, cell in plan["cells"].items():
        if (
            type(cell) is not dict
            or set(cell)
            != {"expected", "arguments", "projection_paths", "mutation_expected"}
            or type(cell["arguments"]) is not dict
            or type(cell["projection_paths"]) is not dict
        ):
            raise ValueError(
                "Native cell must carry exact arguments and independent expectations"
            )
        expected = cell["expected"]
        if (
            type(expected) is not dict
            or not valid_snapshot(
                expected,
                fixture["ledger"].run_id,
                expected.get("operation_id"),
                {"client": client, "tool": tool, "marker": expected.get("marker")},
                baseline=False,
            )
            or cell["arguments"].get("cloudId") != expected["site_id"]
            or expected["operation_id"] in identifiers
            or expected["marker"] in markers
        ):
            raise ValueError("The native cell differs from the owner-bound sentinel")
        if tool in WRITES:
            projection = projected_arguments(
                cell["arguments"], cell["projection_paths"]
            )
            if (
                digest(projection) != expected["content_sha256"]
                or tool != "create_jira_issue"
                and cell["mutation_expected"] != projection
            ):
                raise ValueError(
                    "The native write differs from its independent expectation"
                )
        elif cell["projection_paths"] or cell["mutation_expected"] is not None:
            raise ValueError("Read results must come from the client, not its fixture")
        if expected["provider_id"] == CREATED_JIRA_ID:
            dependent_arguments(tool, cell["arguments"], CREATED_JIRA_ID)
            create = plan["cells"]["create_jira_issue"]["expected"]
            if any(expected[key] != create[key] for key in ("site_id", "account_id")):
                raise ValueError(
                    "Created Jira dependencies must retain the original site and account"
                )
        identifiers.add(expected["operation_id"])
        markers.add(expected["marker"])
    dependent = {
        tool
        for tool, cell in plan["cells"].items()
        if cell["expected"]["provider_id"] == CREATED_JIRA_ID
    }
    if dependent:
        if dependent != JIRA_DEPENDENTS:
            raise ValueError(
                "The Jira sequence must use its single original created issue"
            )
        owned_markers = {plan["cells"][tool]["expected"]["marker"] for tool in WRITES}
        for tool in ("create_jira_issue", "update_jira_issue"):
            cell = plan["cells"][tool]
            summary = projected_arguments(
                cell["arguments"], cell["projection_paths"]
            ).get("summary")
            if (
                not isinstance(summary, str)
                or len(summary) > 255
                or any(marker not in summary for marker in owned_markers)
            ):
                raise ValueError(
                    "Created and updated Jira summaries must retain every owned mutation marker"
                )
        create_hash = plan["cells"]["create_jira_issue"]["expected"]["content_sha256"]
        if any(
            plan["cells"][tool]["expected"]["content_sha256"] != create_hash
            for tool in ("get_jira_issue", "search_jira")
        ):
            raise ValueError(
                "Dependent Jira reads must expect the original creation sentinel"
            )
    return plan


def native_binding(transport_file, tool, arguments):
    transport = json.loads(private_file(transport_file, max_bytes=65536))
    reference = transport["reference"]
    call = {
        "tool": "atlassian___" + tool,
        "arguments_sha256": digest(arguments),
        "max_requests": 1,
    }
    matches = transport["calls"].count(call)
    for planned in transport["calls"]:
        dependency = planned.get("jira_dependency")
        if planned["tool"] != call["tool"] or dependency is None:
            continue
        field = "jql" if tool == "search_jira" else "issueIdOrKey"
        supplied = arguments.get(field, "")
        provider_id = (
            supplied.removeprefix('id = "').removesuffix('"')
            if field == "jql"
            else supplied
        )
        if digest(
            dependent_arguments(tool, dependency["arguments"], provider_id)
        ) == digest(arguments):
            matches += 1
    if matches != 1:
        raise ValueError("Native cell is outside the original transport plan")
    return {"transport_sha256": digest(transport), "reference": reference, **call}


def native_receipt(launch, receipt, binding, arguments):
    if (
        native_binding(
            launch.transport_file,
            binding["tool"].removeprefix("atlassian___"),
            arguments,
        )
        != binding
    ):
        raise ValueError("The original native transport changed")
    journal_path = (
        Path(launch.transport_file).parent
        / binding["reference"]["run_id"]
        / "attempts.json"
    )
    journal = json.loads(private_file(journal_path, max_bytes=262144))
    if journal.get("binding_sha256") != binding["transport_sha256"]:
        raise ValueError("The native journal belongs to another original session")
    matches = [
        entry
        for entry in journal["attempts"]
        if entry.get("kind") == "call"
        and entry.get("receipt", {}).get("request_id") == receipt.get("operation_ref")
    ]
    if len(matches) != 1:
        raise ValueError(
            "Exactly one original native call must confirm the client receipt"
        )
    entry = matches[0]
    payload = {
        "tool": binding["tool"],
        "arguments": arguments,
        "execution_id": None,
        "request_id": receipt["operation_ref"],
    }
    request_value_sha256 = digest(payload)
    if (
        entry.get("status") not in {"response_received", "response_recovered"}
        or entry.get("request_value_sha256") != request_value_sha256
        or entry.get("response_value_sha256")
        != digest(
            {key: value for key, value in receipt.items() if key != "operation_ref"}
        )
        or entry.get("receipt")
        != {
            "request_id": receipt.get("request_id"),
            "tool": binding["tool"],
            "execution_id": receipt.get("execution_id"),
            "receipt_id": receipt.get("receipt_id"),
        }
    ):
        raise ValueError(
            "The client receipt differs from the original native request or journal"
        )
    if entry["status"] == "response_recovered":
        recovered = [
            item
            for item in journal["attempts"]
            if item.get("operation_id") == entry.get("recovered_by")
        ]
        if (
            len(recovered) != 1
            or recovered[0].get("kind") != "recover"
            or recovered[0].get("original_operation_id") != entry["operation_id"]
            or recovered[0].get("status") != "response_received"
            or any(
                recovered[0].get(key) != entry.get(key)
                for key in ("receipt", "response_sha256", "response_value_sha256")
            )
        ):
            raise ValueError("Original native recovery journal is unavailable")
    return {
        "source": "native-transport-journal",
        "binding": binding,
        "operation_ref": receipt["operation_ref"],
        "transport_operation_id": entry["operation_id"],
        **entry["receipt"],
        "request_sha256": entry["request_sha256"],
        "request_value_sha256": request_value_sha256,
        "response_sha256": entry["response_sha256"],
        "journal_entry_sha256": digest(entry),
    }


class NativeMatrix:
    def __init__(
        self,
        plans,
        fixtures,
        *,
        gateway_url,
        gateway_revision,
        client_factory,
        lose_last_write_receipt=False,
        recovery_client=None,
    ):
        required_clients = {recovery_client} if recovery_client in CLIENTS else CLIENTS
        if recovery_client is not None and (
            recovery_client not in CLIENTS
            or not (
                fixtures[recovery_client]["ledger"].path.parent / "recovery-origin.json"
            ).is_file()
        ):
            raise ValueError("Single-client recovery requires restored original state")
        if (
            type(plans) is not dict
            or set(plans) != required_clients
            or set(fixtures) != required_clients
        ):
            raise ValueError("Both native client plans are required")
        self.fixture_ledgers = {}
        plans = {
            client: self._prepare_fixture(plans[client], fixtures[client], client)
            for client in required_clients
        }
        self.plans = {
            client: validate_plan(plans[client], fixtures[client], client)
            for client in required_clients
        }
        cells = [
            cell["expected"]
            for plan in plans.values()
            for cell in plan["cells"].values()
        ]
        if any(
            len({cell[key] for cell in cells}) != len(cells)
            for key in ("operation_id", "marker")
        ):
            raise ValueError(
                "Native clients require globally distinct operation IDs and markers"
            )
        self.fixtures, self.gateway_url, self.gateway_revision = (
            fixtures,
            gateway_url,
            gateway_revision,
        )
        self.client_factory = client_factory
        self.stack, self.clients = ExitStack(), {}
        self.recovery_clients = {}
        self.start_attempts = set()
        self.lose_last_write_receipt = lose_last_write_receipt

    def close(self):
        self.stack.close()
        self.clients.clear()
        self.recovery_clients.clear()

    def _prepare_fixture(self, plan, fixture, client):
        provider = fixture.get("cleanup_provider")
        if provider is None:
            return plan
        if (
            type(provider) is not JiraFixtureProvider
            or provider.owner_id != fixture["owner_id"]
            or provider.run_id != fixture["ledger"].run_id
        ):
            raise ValueError(
                "Separately enabled cleanup authority for this fixture required"
            )
        plan = deepcopy(plan)
        cells = plan["cells"]
        create = cells["create_jira_issue"]
        if (
            create["expected"]["site_id"] != provider.identity["site_id"]
            or create["expected"]["account_id"]
            != provider.config["provider_account_id"]
            or create["arguments"].get("projectKey") != provider.identity["project_key"]
        ):
            raise ValueError(
                "Generated fixture project, site and subject must match the cleanup grant"
            )
        ledger = JiraFixtureLedger(
            fixture["ledger"].path.parent / "jira-fixture.json",
            **provider.identity,
            operation_id=create["expected"]["operation_id"],
        )
        record = ledger.state() or ledger.reserve()
        provider.bind(record)
        self.fixture_ledgers[client] = ledger
        sentinel = (
            "ADP "
            + record["marker"]
            + " "
            + " ".join(cells[tool]["expected"]["marker"] for tool in sorted(WRITES))
        )
        for tool in ("create_jira_issue", "update_jira_issue"):
            cell = cells[tool]
            path = cell["projection_paths"]["summary"].split("/")[1:]
            target = cell["arguments"]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = sentinel + (
                " updated" if tool == "update_jira_issue" else ""
            )
            projection = projected_arguments(
                cell["arguments"], cell["projection_paths"]
            )
            cell["expected"]["content_sha256"] = digest(projection)
            if tool == "update_jira_issue":
                cell["mutation_expected"] = projection
        for tool in JIRA_DEPENDENTS:
            cell = cells[tool]
            cell["expected"]["provider_id"] = CREATED_JIRA_ID
            field = "jql" if tool == "search_jira" else "issueIdOrKey"
            cell["arguments"][field] = (
                'id = "' + CREATED_JIRA_ID + '"' if field == "jql" else CREATED_JIRA_ID
            )
            if tool in READS:
                cell["expected"]["content_sha256"] = create["expected"][
                    "content_sha256"
                ]
        if record["cleanup_status"] == "pending":
            for cell in cells.values():
                ledger.track_operation(cell["expected"]["operation_id"])
        return plan

    def _cell(self, client, tool):
        cell = self.plans[client]["cells"][tool]
        if cell["expected"]["provider_id"] != CREATED_JIRA_ID:
            return cell
        create = self.plans[client]["cells"]["create_jira_issue"]
        original = (
            self.fixtures[client]["ledger"]
            .state()["operations"]
            .get(create["expected"]["operation_id"])
        )
        if (
            original is None
            or original["status"] != "confirmed"
            or original["expected"] != create["expected"]
            or not original.get("provider_id")
            or original.get("native_binding", {}).get("arguments_sha256")
            != digest(create["arguments"])
        ):
            raise ValueError("Independently confirmed original Jira creation required")
        cell = deepcopy(cell)
        cell["expected"]["provider_id"] = original["provider_id"]
        cell["arguments"] = dependent_arguments(
            tool, cell["arguments"], original["provider_id"]
        )
        return cell

    def _start(self, client):
        if client in self.clients:
            return self.clients[client]
        root = self.fixtures[client]["ledger"].path.parent
        if (root / "native-runtime.json").exists():
            (installed, environ, hashes, retained, discovery), _ = self._resume(client)
            transport = json.loads(
                private_file(retained.transport_file, max_bytes=65536)
            )
            session = NativeSession(
                ApprovedReference(**transport["reference"]),
                transport["access_token"],
                transport["capability"],
                None,
            )
            launch, environ = self.stack.enter_context(
                native_client_transport(
                    installed=installed,
                    environ=environ,
                    session=session,
                    transport_file=Path(retained.transport_file),
                    client=client,
                    model=self.plans[client]["model"],
                )
            )
            self.clients[client] = (installed, environ, hashes, launch, discovery)
            return self.clients[client]
        if client in self.start_attempts:
            raise ValueError(
                "Original native setup already attempted; inspect its retained outcome"
            )
        self.start_attempts.add(client)
        plan, fixture = self.plans[client], self.fixtures[client]
        environment = {
            **os.environ,
            "ADP_TOOLS_SMOKE_APPROVAL_FILE": plan["approval_file"],
            "ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE": plan["login_file"],
        }
        session = NativeSession.exchange(environment)
        if (
            session.reference.gateway_origin != self.gateway_url
            or session.reference.source_revision != self.gateway_revision
            or client in self.fixture_ledgers
            and session.reference.tenant_id
            != self.fixture_ledgers[client].identity["tenant_id"]
        ):
            raise ValueError("Original native approval belongs to another deployment")
        root = fixture["ledger"].path.parent
        transport = session.export_client_transport(
            root / "native-transport.json",
            expected_principal={
                "user_id": fixture["owner_id"],
                "tenant_id": session.reference.tenant_id,
            },
            calls=transport_calls(plan),
            models=[
                {
                    "model": plan["model"],
                    "route": "/v1/messages"
                    if client == "claude-code"
                    else "/openai/v1/responses",
                    "max_requests": plan["bounds"]["max_model_requests"]
                    * len(plan["cells"]),
                    **{
                        key: plan["bounds"][key]
                        for key in (
                            "max_request_bytes",
                            "max_response_bytes",
                            "max_output_tokens",
                        )
                    },
                }
            ],
            max_discoveries=9,
        )
        installed, environ, hashes = self.stack.enter_context(
            installed_clients(
                self.gateway_url,
                self.gateway_revision,
                recovery_root=root,
                recovery_id=session.reference.run_id,
            )
        )
        launch, environ = self.stack.enter_context(
            native_client_transport(
                installed=installed,
                environ=environ,
                session=session,
                transport_file=transport,
                client=client,
                model=plan["model"],
            )
        )
        if json.loads(private_file(transport, max_bytes=65536))["reference"] != asdict(
            session.reference
        ):
            raise ValueError(
                "The exported native transport belongs to another approval"
            )
        with self.client_factory(
            [str(installed), "mcp"], environ=environ
        ) as connection:
            listing = connection.request("tools/list")
        tools = listing.get("tools")
        if (
            not isinstance(tools, list)
            or len({tool.get("name") for tool in tools}) != len(tools)
            or not {"atlassian___" + tool for tool in READS | WRITES}
            <= {tool.get("name") for tool in tools}
        ):
            raise ValueError("The admitted client catalogue lacks matrix operations")
        for tool in tools:
            name = tool.get("name", "").removeprefix("atlassian___")
            if name in plan["cells"]:
                arguments, schema = (
                    plan["cells"][name]["arguments"],
                    tool["inputSchema"],
                )
                if (
                    not set(schema.get("required", []))
                    <= set(arguments)
                    <= set(schema.get("properties", {}))
                ):
                    raise ValueError(
                        "Native fixture arguments differ from the admitted schema"
                    )
        version = client_matrix.run_bounded(
            ["claude" if client == "claude-code" else "codex", "--version"],
            environ=environ,
            cwd=environ["HOME"],
            timeout=15,
            max_output_bytes=512,
        )
        if version.returncode or not version.stdout or len(version.stdout) > 100:
            raise ValueError("Native client version unavailable")
        _write_private(
            root / "native-runtime.json",
            {
                "client": client,
                "owner_id": fixture["owner_id"],
                "run_id": fixture["ledger"].run_id,
                "gateway_revision": self.gateway_revision,
                "transport_sha256": digest(
                    json.loads(private_file(transport, max_bytes=65536))
                ),
                "hashes": hashes,
                "discovery_sha256": digest(tools),
                "client_version": version.stdout.decode().strip(),
            },
        )
        self.clients[client] = (installed, environ, hashes, launch, digest(tools))
        return self.clients[client]

    def _resume(self, client):
        fixture = self.fixtures[client]
        root = fixture["ledger"].path.parent
        transport_file = root / "native-transport.json"
        transport = json.loads(private_file(transport_file, max_bytes=65536))
        metadata = json.loads(
            private_file(root / "native-runtime.json", max_bytes=65536)
        )
        if (
            metadata["client"] != client
            or metadata["owner_id"] != fixture["owner_id"]
            or metadata["run_id"] != fixture["ledger"].run_id
            or metadata["gateway_revision"] != self.gateway_revision
            or metadata["transport_sha256"] != digest(transport)
            or transport["reference"]["gateway_origin"] != self.gateway_url
            or transport["reference"]["source_revision"] != self.gateway_revision
        ):
            raise ValueError("Original native runtime binding changed")
        if client in self.clients:
            return self.clients[client], metadata
        if client not in self.recovery_clients:
            installed, environ, hashes = self.stack.enter_context(
                resume_installed_clients(
                    self.gateway_url,
                    self.gateway_revision,
                    recovery_root=root,
                    recovery_id=transport["reference"]["run_id"],
                )
            )
            if hashes != metadata["hashes"]:
                raise ValueError("Original installed client hashes changed")
            environ = {**environ, "ADP_NATIVE_TEST_TRANSPORT_FILE": str(transport_file)}
            self.recovery_clients[client] = (
                installed,
                environ,
                hashes,
                SimpleNamespace(transport_file=str(transport_file)),
                metadata["discovery_sha256"],
            )
        return self.recovery_clients[client], metadata

    def _retained_native(self, client, tool, launch, environ, installed, metadata):
        fixture, cell = self.fixtures[client], self._cell(client, tool)
        operation_id = cell["expected"]["operation_id"]
        root = fixture["ledger"].path.parent
        retained = root / ("native-result-" + digest(operation_id) + ".json")
        if retained.exists():
            return json.loads(private_file(retained, max_bytes=524288))
        binding = fixture["ledger"].state()["operations"][operation_id][
            "native_binding"
        ]
        journal = json.loads(
            private_file(
                root / binding["reference"]["run_id"] / "attempts.json",
                max_bytes=262144,
            )
        )
        candidates = [
            entry
            for entry in journal["attempts"]
            if entry.get("kind") == "call"
            and entry.get("request_id")
            and entry.get("request_value_sha256")
            == digest(
                {
                    "tool": "atlassian___" + tool,
                    "arguments": cell["arguments"],
                    "execution_id": None,
                    "request_id": entry["request_id"],
                }
            )
        ]
        if (
            len(candidates) != 1
            or journal["binding_sha256"] != binding["transport_sha256"]
        ):
            raise ValueError(
                "Exactly one original native request is required for recovery"
            )
        operation_ref = candidates[0]["request_id"]
        with self.client_factory(
            [str(installed), "mcp"], environ=environ
        ) as connection:
            packet = connection.request(
                "tools/call",
                {
                    "name": "adp___operation_status",
                    "arguments": {"operation_ref": operation_ref},
                },
            )
        contents = packet.get("content")
        if (
            packet.get("isError") is True
            or not isinstance(contents, list)
            or len(contents) != 1
            or contents[0].get("type") != "text"
        ):
            raise ValueError("Original operation is still unresolved")
        receipt = json.loads(contents[0]["text"])
        if receipt.get("operation_ref") != operation_ref:
            raise ValueError("Recovered operation differs from the original request")
        native_receipt(launch, receipt, binding, cell["arguments"])
        result = {
            "private_receipt": receipt,
            "operation_ref": operation_ref,
            "execution_id": receipt["execution_id"],
            "receipt_id": receipt["receipt_id"],
            "receipt_sha256": digest(receipt),
            "client_version": metadata["client_version"],
        }
        _write_private(retained, result)
        return result

    def _observe(self, client, tool, launch, receipt):
        fixture = self.fixtures[client]
        ledger, observer = fixture["ledger"], fixture["reader"]
        cells = self.plans[client]["cells"]
        operations = ledger.state()["operations"]
        update = operations.get(cells["update_jira_issue"]["expected"]["operation_id"])
        if (
            tool not in {"create_jira_issue", "get_jira_issue", "search_jira"}
            or cells["update_jira_issue"]["expected"]["provider_id"] != CREATED_JIRA_ID
            or update is None
            or update["status"] != "confirmed"
        ):
            return observe_cell(ledger, observer, client=client, tool=tool)
        original = operations[cells[tool]["expected"]["operation_id"]]
        expected = {**original["expected"], "provider_id": original["provider_id"]}
        if (
            original.get("provider_observation") != expected
            or original.get("native_receipt") != receipt
            or original["provider_id"] != update["provider_id"]
        ):
            raise ValueError(
                "Original per-step provider observation and native receipt required"
            )
        update_cell = self._cell(client, "update_jira_issue")
        retained = json.loads(
            private_file(
                ledger.path.parent
                / (
                    "native-result-"
                    + digest(update_cell["expected"]["operation_id"])
                    + ".json"
                ),
                max_bytes=524288,
            )
        )
        update_receipt = native_receipt(
            launch,
            retained["private_receipt"],
            update["native_binding"],
            update_cell["arguments"],
        )
        if update_receipt != update["native_receipt"]:
            raise ValueError("The later update differs from its original receipt")
        journal = json.loads(
            private_file(
                Path(launch.transport_file).parent
                / receipt["binding"]["reference"]["run_id"]
                / "attempts.json",
                max_bytes=262144,
            )
        )
        order = [
            entry["operation_id"]
            for entry in journal["attempts"]
            if entry["kind"] == "call"
        ]
        if order.index(receipt["transport_operation_id"]) >= order.index(
            update_receipt["transport_operation_id"]
        ):
            raise ValueError("The retained sentinel must precede the owned update")
        create = cells["create_jira_issue"]
        projection = projected_arguments(
            create["arguments"], create["projection_paths"]
        )
        if expected["content_sha256"] != digest(projection) or update[
            "mutation_baseline"
        ]["resource"] != {"summary": projection["summary"]}:
            raise ValueError(
                "The update history does not start at the original sentinel"
            )
        observe_cell(ledger, observer, client=client, tool="update_jira_issue")
        current = observer.readback(
            **{
                key: expected[key]
                for key in (
                    "run_id",
                    "client",
                    "tool",
                    "operation_id",
                    "marker",
                    "provider_id",
                    "site_id",
                )
            }
        )
        current_projection = {**projection, **update["mutation_expected"]}
        if current != {**expected, "content_sha256": digest(current_projection)}:
            raise ValueError(
                "Current provider content differs from the verified owned history"
            )
        return {
            "source": "confirmed-client-operation-and-direct-provider",
            "operation_id": expected["operation_id"],
            "content_sha256": expected["content_sha256"],
            "effect_count": expected["effect_count"],
            "history_revalidated": True,
        }

    def _finalized_path(self, client):
        return self.fixtures[client]["ledger"].path.parent / "native-finalized.json"

    def _cleanup_proof(self, client):
        fixture = self.fixtures[client]
        ledger = fixture["ledger"]
        state = ledger.state()
        plans = self.plans[client]["cells"]
        expected_ids = {cell["expected"]["operation_id"] for cell in plans.values()}
        if not set(state["operations"]) <= expected_ids:
            raise ValueError("Only the original matrix operations may be finalized")
        (_, _, _, launch, _), metadata = self._resume(client)
        evidence = {}
        for tool in plans:
            operation_id = plans[tool]["expected"]["operation_id"]
            row = state["operations"].get(operation_id)
            if row is None or row["status"] == "pending" and tool in READS:
                continue
            cell = self._cell(client, tool)
            if row["status"] != "confirmed" or "provider_observation" not in row:
                raise ValueError(
                    "Every original operation requires a retained independent observation"
                )
            original = json.loads(
                private_file(
                    ledger.path.parent
                    / ("native-result-" + digest(operation_id) + ".json"),
                    max_bytes=524288,
                )
            )
            receipt = native_receipt(
                launch,
                original["private_receipt"],
                row["native_binding"],
                cell["arguments"],
            )
            response = client_result(
                original["private_receipt"], cell, fixture["owner_id"]
            )
            if receipt != row["native_receipt"] or response != {
                key: row["provider_observation"][key]
                for key in ("provider_id", "site_id", "account_id", "content_sha256")
            }:
                raise ValueError(
                    "Original client receipt and independent provider evidence disagree"
                )
            closed = json.loads(
                private_file(
                    ledger.path.parent
                    / ("native-close-" + digest(operation_id) + ".json"),
                    max_bytes=65536,
                )
            )
            if closed["binding"] != row["native_binding"]:
                raise ValueError("Original execution close binding differs")
            recovery_result(
                closed["packet"],
                "adp___execution_close",
                original,
                {"operation_ref": original["operation_ref"]},
            )
            evidence[tool] = {
                "original": original,
                "closed": closed,
                "receipt": receipt,
            }
        transport = json.loads(private_file(launch.transport_file, max_bytes=65536))
        journal = json.loads(
            private_file(
                Path(launch.transport_file).parent
                / transport["reference"]["run_id"]
                / "attempts.json",
                max_bytes=262144,
            )
        )
        confirmed_writes = {
            value["receipt"]["transport_operation_id"]
            for tool, value in evidence.items()
            if tool in WRITES
        }
        for entry in journal["attempts"]:
            if entry["kind"] == "call":
                tool = transport["calls"][int(entry["slot"].split(":")[1])][
                    "tool"
                ].removeprefix("atlassian___")
                if tool in WRITES and entry["operation_id"] not in confirmed_writes:
                    raise ValueError(
                        "Every attempted provider mutation must be independently confirmed before deletion"
                    )
        record = self.fixture_ledgers[client].state()
        proof = digest(
            {
                "operations": state,
                "runtime": metadata,
                "evidence": evidence,
                "fixture": {
                    key: value
                    for key, value in record.items()
                    if key != "cleanup_status"
                },
                "provider": fixture["cleanup_provider"].config,
            }
        )
        return proof, evidence, metadata

    def finalize(self, client):
        if client not in self.fixture_ledgers:
            return {"status": "pending", "provider_fixtures": "pending"}
        try:
            path = self._finalized_path(client)
            proof, _, _ = self._cleanup_proof(client)
            if path.exists():
                if json.loads(private_file(path, max_bytes=4096)) != {
                    "version": 1,
                    "proof_sha256": proof,
                }:
                    raise ValueError("Finalized matrix evidence changed")
            else:
                _, evidence, _ = self._cleanup_proof(client)
                complete = len(evidence) == len(self.plans[client]["cells"])
                for tool in evidence:
                    if not complete and tool in READS:
                        continue
                    result = self.execute(client, tool)
                    if (
                        result["status"] != "pass"
                        or result["cleanup"].get("execution") != "confirmed"
                    ):
                        raise ValueError(
                            "Current provider effects must be reverified before cleanup"
                        )
                fresh_proof, _, _ = self._cleanup_proof(client)
                if fresh_proof != proof:
                    raise ValueError(
                        "Original matrix evidence changed during final verification"
                    )
                _write_private(path, {"version": 1, "proof_sha256": proof})
            ledger = self.fixtures[client]["ledger"]
            result = self.fixture_ledgers[client].cleanup(
                self.fixtures[client]["cleanup_provider"],
                pending_operations=lambda operation: (
                    row := ledger.state()["operations"].get(operation)
                )
                is not None
                and row["tool"] in WRITES
                and row["status"] != "confirmed",
            )
            if result != {"status": "confirmed", "owned_absent": True}:
                raise ValueError("Independent fixture absence is unconfirmed")
            return {
                "status": "confirmed",
                "execution": "confirmed",
                "provider_fixtures": "confirmed",
            }
        except (ValueError, KeyError, OSError, TimeoutError):
            return {"status": "pending", "provider_fixtures": "pending"}

    def _finalized_result(self, client, tool):
        cleanup = self.finalize(client)
        if cleanup["status"] != "confirmed":
            raise ValueError("Independent cleanup must be reverified")
        _, evidence, metadata = self._cleanup_proof(client)
        if tool not in evidence:
            return {
                "status": "blocked",
                "reason": "The original incomplete matrix was finalized without this cell passing",
                "cleanup": cleanup,
            }
        original = evidence[tool]["original"]
        expected = self._cell(client, tool)["expected"]
        return {
            "status": "pass",
            "reason": "Original per-step proof retained and provider absence independently reverified",
            "client_version": original["client_version"],
            "cleanup": cleanup,
            "observations": {
                "source": "confirmed-client-operation-and-direct-provider",
                "operation_id": expected["operation_id"],
                "content_sha256": expected["content_sha256"],
                "effect_count": expected["effect_count"],
                "original_operation_recovered": True,
                "cleanup_revalidated": True,
                "installed_cli_sha256": metadata["hashes"]["adp"],
                "installed_mcp_sha256": metadata["hashes"]["adp-mcp.py"],
                "discovery_sha256": metadata["discovery_sha256"],
                "receipt_sha256": original["receipt_sha256"],
                "provider_observation": {"source": "direct-atlassian-provider"},
            },
        }

    def execute(self, client, tool):
        result = {
            "status": "blocked",
            "reason": "Native admission or independent fixture unavailable",
            "cleanup": {"status": "not-started", "provider_fixtures": "pending"},
        }
        if client in self.fixture_ledgers and self._finalized_path(client).exists():
            try:
                return self._finalized_result(client, tool)
            except (ValueError, KeyError, OSError, TimeoutError):
                result.update(
                    status="unknown",
                    reason="Finalized fixture evidence or independent cleanup remains unconfirmed",
                )
                return result
        try:
            fixture, cell = self.fixtures[client], self._cell(client, tool)
        except ValueError:
            result["reason"] = "Independently confirmed original Jira creation required"
            return result
        ledger, observer, expected = (
            fixture["ledger"],
            fixture["reader"],
            cell["expected"],
        )
        operation_id = expected["operation_id"]
        retained_operation = ledger.state()["operations"].get(operation_id)
        if (
            retained_operation is None
            and (ledger.path.parent / "recovery-origin.json").exists()
        ):
            result["reason"] = "Restored recovery cannot authorize an unattempted cell"
            return result
        if retained_operation is None and any(
            row["status"] == "pending" for row in ledger.state()["operations"].values()
        ):
            result["reason"] = (
                "Recover the pending original operation before reserving another cell"
            )
            return result
        try:
            if retained_operation is not None:
                result.update(
                    status="unknown",
                    reason="Original operation retained; replay is prohibited",
                )
                (installed, environ, hashes, launch, discovery), metadata = (
                    self._resume(client)
                )
                binding = retained_operation["native_binding"]
            else:
                if client in self.fixture_ledgers and tool == "create_jira_issue":
                    provider = fixture["cleanup_provider"]
                    provider.authorize()
                    record = self.fixture_ledgers[client].state()
                    existing = provider.read_issues(
                        site_id=record["site_id"],
                        project_key=record["project_key"],
                        marker=record["marker"],
                    )
                    if existing != {"complete": True, "issues": []}:
                        raise ValueError("An existing fixture cannot be created again")
                installed, environ, hashes, launch, discovery = self._start(client)
                binding = native_binding(launch.transport_file, tool, cell["arguments"])
                ledger.reserve(
                    client=client,
                    tool=tool,
                    operation_id=operation_id,
                    marker=expected["marker"],
                    provider_id=expected["provider_id"],
                    site_id=expected["site_id"],
                    expected=expected,
                    observer=observer,
                    mutation_expected=cell["mutation_expected"],
                    native_binding=binding,
                )
        except (
            ValueError,
            KeyError,
            OSError,
            TimeoutError,
            subprocess.SubprocessError,
        ):
            return result
        result.update(
            status="unknown", reason="Original native dispatch remains unconfirmed"
        )
        native = None
        confirmed_native = None
        try:
            if retained_operation is not None:
                native = self._retained_native(
                    client, tool, launch, environ, installed, metadata
                )
            else:
                native = native_call(
                    client,
                    installed,
                    environ,
                    "atlassian___" + tool,
                    cell["arguments"],
                    model=self.plans[client]["model"],
                    timeout=120,
                    bounds=self.plans[client]["bounds"],
                    native_launch=launch,
                    retain_receipt=True,
                )
            receipt = native_receipt(
                launch, native["private_receipt"], binding, cell["arguments"]
            )
            fault = ledger.path.parent / "native-receipt-loss.json"
            if (
                self.lose_last_write_receipt
                and tool == "transition_jira_issue"
                and not fault.exists()
            ):
                _write_private(
                    fault, {"operation_id": operation_id, "receipt": receipt}
                )
                raise TimeoutError(
                    "Controlled loss of the final planned write response"
                )
            confirmed_native = native
            retained_path = ledger.path.parent / (
                "native-result-" + digest(operation_id) + ".json"
            )
            if not retained_path.exists():
                _write_private(retained_path, native)
            response = client_result(
                native["private_receipt"], cell, fixture["owner_id"]
            )
            if retained_operation is None or retained_operation["status"] == "pending":
                ledger.confirm(
                    operation_id=operation_id,
                    client_result=response,
                    observer=observer,
                    receipt=receipt,
                )
            if client in self.fixture_ledgers and tool == "create_jira_issue":
                self.fixture_ledgers[client].confirm_created(
                    fixture["cleanup_provider"], response["provider_id"]
                )
            observation = self._observe(client, tool, launch, receipt)
            result.update(
                status="pass",
                reason=(
                    "Original native operation recovered without replay and independently confirmed"
                    if retained_operation is not None
                    else "Native client result and independent provider readback match"
                ),
                client_version=native["client_version"],
                observations={
                    **observation,
                    "original_operation_recovered": retained_operation is not None,
                    "installed_cli_sha256": hashes["adp"],
                    "installed_mcp_sha256": hashes["adp-mcp.py"],
                    "discovery_sha256": discovery,
                    "receipt_sha256": native["receipt_sha256"],
                    "provider_observation": {"source": "direct-atlassian-provider"},
                },
            )
        except (
            ValueError,
            KeyError,
            OSError,
            TimeoutError,
            subprocess.SubprocessError,
        ):
            pass
        finally:
            if confirmed_native is not None:
                try:
                    arguments = {"operation_ref": native["operation_ref"]}
                    closed_path = ledger.path.parent / (
                        "native-close-" + digest(operation_id) + ".json"
                    )
                    if closed_path.exists():
                        closed = json.loads(private_file(closed_path, max_bytes=65536))
                        if closed["binding"] != binding:
                            raise ValueError(
                                "Original execution cleanup binding changed"
                            )
                        packet = closed["packet"]
                    else:
                        with self.client_factory(
                            [str(installed), "mcp"], environ=environ
                        ) as connection:
                            packet = connection.request(
                                "tools/call",
                                {
                                    "name": "adp___execution_close",
                                    "arguments": arguments,
                                },
                            )
                    recovery_result(packet, "adp___execution_close", native, arguments)
                    if not closed_path.exists():
                        _write_private(
                            closed_path, {"binding": binding, "packet": packet}
                        )
                    result["cleanup"] = {
                        "status": "pending",
                        "execution": "confirmed",
                        "provider_fixtures": "pending",
                    }
                except (ValueError, KeyError, OSError, TimeoutError):
                    result["cleanup"]["execution"] = "unknown"
                    result.update(
                        status="unknown",
                        reason="Original execution cleanup remains unconfirmed",
                    )
            else:
                result["cleanup"]["execution"] = "unknown"
        return result


def load_native_plans(path):
    from tests.e2e.tools.atlassian_client_matrix import recovery_roots

    source = Path(path)
    value = json.loads(private_file(source, max_bytes=131072))
    if type(value) is not dict or set(value) != CLIENTS:
        raise ValueError("Both private native plans are required")
    restored = recovery_roots(source.parent)
    for client, plan in value.items():
        if client in restored:
            original = json.loads(
                private_file(restored[client] / "recovery-plan.json", max_bytes=131072)
            )
            journal = json.loads(
                private_file(
                    restored[client] / "cleanup-consumer.json", max_bytes=262144
                )
            )
            origin = json.loads(
                private_file(restored[client] / "recovery-origin.json", max_bytes=8192)
            )
            if (
                digest(original) != journal["binding"]["plan_sha256"]
                or digest(journal["binding"]) != origin["binding_sha256"]
            ):
                raise ValueError(
                    "Restored original plan differs from its retained authority"
                )
            value[client] = original
            continue
        if type(plan) is not dict:
            raise ValueError("Private native plan is invalid")
        for key, name in (
            ("approval_file", "native-approval.json"),
            ("login_file", "native-login.token"),
        ):
            if plan.get(key) != str(source.parent / client / name):
                raise ValueError(
                    "Native authority must remain in the isolated client directory"
                )
            private_file(plan[key], max_bytes=16384)
    return value
