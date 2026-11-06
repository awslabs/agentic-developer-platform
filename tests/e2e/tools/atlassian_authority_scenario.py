"""Protected installed-client authority probes with independent MCP readback.

Ordinary CLI sessions preserve explicit HTTP denials. The success-only native
test transport deliberately turns non-200 responses into unresolved operations
and must not be used to certify a denial. Provider tokens stay inside the
existing Identity readback callback; two successful token reads do not prove
expiry or managed refresh.
"""

from contextlib import ExitStack
from copy import deepcopy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from types import SimpleNamespace
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx

from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools.atlassian_client_matrix import HANDLE, MARKER
from tests.e2e.tools.atlassian_readback_grant import (
    IdentityReadbackGrant,
    CALL_BINDINGS,
)
from tests.e2e.tools.atlassian_policy_observation import policy_denied
from tests.e2e.tools.atlassian_fixture_cleanup import JiraFixtureLedger
from tests.e2e.tools.atlassian_fixture_provider import JiraFixtureProvider
from tests.e2e.tools.client_matrix import import_session
from tests.e2e.tools.installed_clients import (
    installed_clients,
    resume_installed_clients,
    configure_model_transport,
)
from tests.e2e.tools.process_bounds import run_bounded
from modules.tools.platform.scripts.native_test_session import private_file
from tool_adapters.atlassian_observe import collect_observation


CLIENTS = ("claude-code", "codex")
DENIALS = (
    "read_only",
    "foreign_owner",
    "foreign_site",
    "expired",
    "disconnected",
    "policy_denied",
)
CASES = (*DENIALS, "identity_refresh", "catalogue_schema")
BOUNDS = {
    "max_model_requests": 4,
    "max_input_tokens": 65536,
    "max_output_tokens": 8192,
    "max_request_bytes": 65536,
    "max_response_bytes": 262144,
}
MAX_BYTES = 4 * 1024 * 1024
READ_TOOLS = {"atlassian___get_jira_issue", "atlassian___get_confluence_page"}
OWNER_DENIAL = "Original login, deployment or run binding is missing or changed; restore it before recovery."


def digest(value):
    return hashlib.sha256(flow.canonical(value)).hexdigest()


def _json_copy(value):
    if len(flow.canonical(value)) > MAX_BYTES:
        raise ValueError("Authority evidence exceeds its bound")
    return deepcopy(value)


def _write(path, value):
    data = flow.canonical(value)
    if len(data) > MAX_BYTES:
        raise ValueError("Authority journal exceeds its bound")
    descriptor, temporary = tempfile.mkstemp(prefix=".authority-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def native_observation(client, transcript, *, tool, arguments, bounds):
    """Validate one native call, including a genuine MCP rejection response.

    Terminal model errors, alternate tools, unbounded usage and uncertain tool
    replies are not denials. Raw transcripts belong only in private evidence.
    """
    if (
        client not in CLIENTS
        or type(transcript) is not str
        or len(transcript.encode()) > bounds["max_response_bytes"]
    ):
        raise ValueError("Bounded native transcript required")
    events = [flow._decode(line) for line in transcript.splitlines() if line.strip()]
    if not events or any(type(event) is not dict for event in events):
        raise ValueError("Native transcript is empty or malformed")
    if client == "claude-code":
        if any(
            event.get("type")
            not in {"system", "assistant", "user", "result", "rate_limit_event"}
            for event in events
        ):
            raise ValueError("Alternate native execution")
        terminal = [event for event in events if event.get("type") == "result"]
        calls = [
            item
            for event in events
            if event.get("type") == "assistant"
            for item in event.get("message", {}).get("content", [])
            if item.get("type") == "tool_use"
        ]
        replies = [
            item
            for event in events
            if event.get("type") == "user"
            for item in event.get("message", {}).get("content", [])
            if item.get("type") == "tool_result"
        ]
        if (
            len(terminal) != 1
            or terminal[0].get("is_error") is True
            or len(calls) != 1
            or len(replies) != 1
            or calls[0].get("name") != "mcp__adp-tools__" + tool
            or not calls[0].get("id")
            or replies[0].get("tool_use_id") != calls[0]["id"]
        ):
            raise ValueError("One completed native MCP attempt required")
        used, packet = calls[0].get("input"), replies[0].get("content")
        usage = [
            event["message"]["usage"]
            for event in events
            if event.get("type") == "assistant"
            and type(event.get("message", {}).get("usage")) is dict
        ]
    else:
        if any(
            event.get("type")
            not in {
                "thread.started",
                "turn.started",
                "item.started",
                "item.updated",
                "item.completed",
                "turn.completed",
            }
            for event in events
        ):
            raise ValueError("Alternate native execution")
        items = [event["item"] for event in events if type(event.get("item")) is dict]
        if any(
            item.get("type") not in {"mcp_tool_call", "agent_message", "reasoning"}
            for item in items
        ):
            raise ValueError("Alternate native tool execution")
        calls = [
            event["item"]
            for event in events
            if event.get("type") == "item.completed"
            and event.get("item", {}).get("type") == "mcp_tool_call"
        ]
        if len(calls) != 1 or not calls[0].get("id"):
            raise ValueError("One native MCP attempt required")
        call = calls[0]
        if any(
            item.get("server") != "adp-tools"
            or item.get("tool") != tool
            or item.get("id") != call["id"]
            for item in items
            if item.get("type") == "mcp_tool_call"
        ):
            raise ValueError("Alternate or duplicate native MCP attempt")
        used, packet = call.get("arguments"), call.get("result")
        if isinstance(used, str):
            used = flow._decode(used)
        usage = [
            event["usage"]
            for event in events
            if event.get("type") == "turn.completed"
            and type(event.get("usage")) is dict
        ]
    if (
        flow.canonical(used) != flow.canonical(arguments)
        or not 0 < len(usage) <= bounds["max_model_requests"]
        or any(
            type(item.get(key)) is not int or item[key] < 0
            for item in usage
            for key in ("input_tokens", "output_tokens")
        )
        or sum(item["input_tokens"] for item in usage) > bounds["max_input_tokens"]
        or sum(item["output_tokens"] for item in usage) > bounds["max_output_tokens"]
    ):
        raise ValueError("Native arguments or usage differ from the protected bound")
    if type(packet) is dict and "content" in packet:
        packet = packet["content"]
    if type(packet) is str:
        packet = [{"type": "text", "text": packet}]
    if (
        type(packet) is not list
        or len(packet) != 1
        or type(packet[0]) is not dict
        or packet[0].get("type") != "text"
        or type(packet[0].get("text")) is not str
    ):
        raise ValueError("Native MCP operation response unavailable")
    response = flow._decode(packet[0]["text"])
    if type(response) is not dict:
        raise ValueError("Native MCP operation response is invalid")
    return {
        "source": client + "-native-mcp",
        "tool": tool,
        "arguments_sha256": digest(arguments),
        "transcript_sha256": hashlib.sha256(transcript.encode()).hexdigest(),
        "model_requests": len(usage),
        "input_tokens": sum(item["input_tokens"] for item in usage),
        "output_tokens": sum(item["output_tokens"] for item in usage),
        "response": response,
    }


def invoke_native(client, installed, environ, probe, spec):
    prompt = (
        "Invoke exactly the adp-tools MCP operation "
        + probe["tool"]
        + " once using these arguments: "
        + json.dumps(probe["arguments"], separators=(",", ":"))
        + ". Report its result, including any denial. Do not retry or invoke another tool."
    )
    bounds = spec["bounds"]
    if len(prompt.encode()) > bounds["max_request_bytes"]:
        raise ValueError("Native request exceeds protected bound")
    launcher = [str(installed), "claude" if client == "claude-code" else "codex"]
    command = (
        [
            *launcher,
            "-p",
            "--model",
            spec["model"],
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            "mcp__adp-tools__" + probe["tool"],
        ]
        if client == "claude-code"
        else [
            *launcher,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-m",
            spec["model"],
            "-",
        ]
    )
    result = run_bounded(
        command,
        input_data=prompt.encode(),
        environ=environ,
        cwd=environ["HOME"],
        timeout=120,
        max_input_bytes=bounds["max_request_bytes"],
        max_output_bytes=bounds["max_response_bytes"],
    )
    if result.returncode:
        raise ValueError("Native client did not complete")
    transcript = result.stdout.decode()
    return transcript, native_observation(
        client,
        transcript,
        tool=probe["tool"],
        arguments=probe["arguments"],
        bounds=bounds,
    )


def validate_manifest(manifest, *, binding, gateway_url, account, region, reader_role):
    flow.validate_binding(binding)
    if (
        type(manifest) is not dict
        or set(manifest) != {"binding", "gateway_url", "tenant_id", "clients"}
        or manifest["binding"] != binding
        or manifest["gateway_url"] != gateway_url
        or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", gateway_url)
        or type(manifest["tenant_id"]) is not str
        or not HANDLE.fullmatch(manifest["tenant_id"])
        or type(manifest["clients"]) is not dict
        or set(manifest["clients"]) != set(CLIENTS)
    ):
        raise ValueError(
            "Protected authority manifest differs from the selected release"
        )
    owners, accounts = set(), set()
    for client, spec in manifest["clients"].items():
        if (
            type(spec) is not dict
            or set(spec)
            not in (
                {"owner_id", "model", "bounds", "grant", "probes"},
                {"owner_id", "model", "bounds", "grant", "probes", "owned_fixture"},
            )
            or type(spec["model"]) is not str
            or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", spec["model"])
            or type(spec["bounds"]) is not dict
            or set(spec["bounds"]) != set(BOUNDS)
            or any(
                type(spec["bounds"][key]) is not int
                or not 0 < spec["bounds"][key] <= ceiling
                for key, ceiling in BOUNDS.items()
            )
            or type(spec["probes"]) is not dict
            or set(spec["probes"]) != set(CASES)
        ):
            raise ValueError("Complete protected client authority inventory required")
        grant = IdentityReadbackGrant(spec["grant"], spec["owner_id"])
        if any(
            grant.config[key] != value
            for key, value in {
                "account": account,
                "region": region,
                "adapter_role_arn": reader_role,
            }.items()
        ):
            raise ValueError("Independent Identity reader authority differs")
        owners.add(grant.owner_id)
        accounts.add(grant.config["provider_account_id"])
        for variant, probe in spec["probes"].items():
            if probe is None:
                continue
            if (
                type(probe) is not dict
                or set(probe)
                != {
                    "tool",
                    "arguments",
                    "subject_client",
                    "site_id",
                    "provider_id",
                    "marker",
                }
                or probe["subject_client"] not in CLIENTS
                or probe["tool"]
                not in (
                    READ_TOOLS
                    | (
                        {"atlassian___update_jira_issue"}
                        if variant == "read_only"
                        else set()
                    )
                )
                or type(probe["arguments"]) is not dict
                or any(
                    type(probe[key]) is not str or not HANDLE.fullmatch(probe[key])
                    for key in ("site_id", "provider_id")
                )
                or type(probe["marker"]) is not str
                or not MARKER.fullmatch(probe["marker"])
            ):
                raise ValueError("A bound, read-only authority probe is required")
            if probe["tool"] in READ_TOOLS:
                operation = "page" if "confluence" in probe["tool"] else "issue"
                for field, candidates in CALL_BINDINGS[operation].items():
                    selected = [
                        name for name in candidates if name in probe["arguments"]
                    ]
                    if (
                        len(selected) != 1
                        or probe["arguments"][selected[0]] != probe[field]
                    ):
                        raise ValueError(
                            "Native arguments must name the independently observed resource"
                        )
                if set(probe["arguments"]) - {
                    name
                    for values in CALL_BINDINGS[operation].values()
                    for name in values
                }:
                    raise ValueError("Authority read has unqualified arguments")
            if (
                variant in {"foreign_owner", "foreign_site"}
                and probe["subject_client"] == client
            ):
                raise ValueError("A distinct subject owner is required")
            if variant == "read_only" and probe["subject_client"] != client:
                raise ValueError("Write denial must use the fixture owner")
            if (
                variant == "read_only"
                and probe["tool"] == "atlassian___update_jira_issue"
            ):
                _validate_owned_fixture(spec, probe, binding)
                if (
                    spec["owned_fixture"]["record"]["tenant_id"]
                    != manifest["tenant_id"]
                ):
                    raise ValueError("Write fixture belongs to another tenant")
    if len(owners) != 2 or len(accounts) != 2:
        raise ValueError("Two distinct ADP owners and provider accounts required")
    return _json_copy(manifest)


def _validate_owned_fixture(spec, probe, binding):
    owned = spec.get("owned_fixture")
    if type(owned) is not dict or set(owned) != {"record", "cleanup"}:
        raise ValueError(
            "A previously generated owned Jira fixture and cleanup grant are required"
        )
    record = owned["record"]
    if (
        type(record) is not dict
        or record.get("owner_id") != spec["owner_id"]
        or record.get("run_id") != binding["run_id"]
        or record.get("operation_ids") != [record.get("operation_id")]
        or record.get("site_id") != probe["site_id"]
        or record.get("issue_ids") != [probe["provider_id"]]
        or record.get("create_status") != "confirmed"
        or record.get("cleanup_status") != "pending"
        or type(record.get("marker")) is not str
        or not re.fullmatch(r"adp-jira-[A-Za-z0-9_-]{43}", record["marker"])
        or probe["arguments"].get("cloudId") != probe["site_id"]
        or probe["arguments"].get("issueIdOrKey") != probe["provider_id"]
        or set(probe["arguments"]) != {"cloudId", "issueIdOrKey", "fields"}
        or probe["arguments"]["fields"]
        != {"summary": record["marker"] + " authority denial probe"}
    ):
        raise ValueError(
            "Write denial must target only the generated run-owned Jira issue"
        )
    JiraFixtureProvider(
        owned["cleanup"],
        readback=spec["grant"],
        owner_id=spec["owner_id"],
        run_id=binding["run_id"],
    )


def bind_authority(
    raw, sessions, *, directory, binding, gateway_url, account, region, reader_role
):
    """Materialize data-only bindings; never import an operator-supplied module."""
    if len(raw.encode()) > 65536 or len(sessions.encode()) > 32768:
        raise ValueError("Protected authority input exceeds its bound")
    manifest = validate_manifest(
        flow._decode(raw),
        binding=binding,
        gateway_url=gateway_url,
        account=account,
        region=region,
        reader_role=reader_role,
    )
    tokens = flow._decode(sessions)
    if (
        type(tokens) is not dict
        or set(tokens) != set(CLIENTS)
        or any(
            type(token) is not str
            or not token
            or len(token) > 16384
            or any(c.isspace() for c in token)
            for token in tokens.values()
        )
        or len(set(tokens.values())) != 2
    ):
        raise ValueError("Two private, distinct ordinary ADP refresh sessions required")
    root = Path(tempfile.mkdtemp(prefix="atlassian-authority-", dir=directory))
    try:
        for client, token in tokens.items():
            (root / client).mkdir(mode=0o700)
            path = root / client / "session.token"
            with os.fdopen(
                os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as stream:
                stream.write(token)
        _write(root / "config.json", manifest)
        return root / "config.json"
    except Exception:
        shutil.rmtree(root)
        raise


def provider_snapshot(grant, probe, *, capture_policy=False):
    """Read current account/sites and the independently selected resource via MCP."""

    def observe(session):
        settings = SimpleNamespace(
            **{
                key: grant.config[key]
                for key in ("subject_schema_sha256", "sites_schema_sha256")
            }
        )
        subject, sites, _ = collect_observation(session.token, settings)
        if subject != grant.config["provider_account_id"]:
            raise ValueError("Independent provider account changed")
        product = "confluence" if "confluence" in probe["tool"] else "jira"
        suffix = (
            "wiki/api/v2/pages/" if product == "confluence" else "rest/api/3/issue/"
        ) + quote(probe["provider_id"], safe="")
        snapshot = {
            "source": "direct-atlassian-provider",
            "owner_id": grant.owner_id,
            "account_id": subject,
            "sites": [
                {"site_id": site.site_id, "scopes": sorted(site.scopes)}
                for site in sites
            ],
            "site_id": probe["site_id"],
            "provider_id": probe["provider_id"],
        }
        captured = []
        transport = session.transport

        class Capture:
            def send(self, method, params, token):
                if method != "tools/call" or captured:
                    raise ValueError("One independent provider read required")
                reply = transport.send(method, params, token)
                captured.append(_json_copy(reply))
                return reply

        if capture_policy:
            session.transport = Capture()
        try:
            resource = session.get(
                f"/ex/{product}/{quote(probe['site_id'], safe='')}/{suffix}"
            )
        except ValueError:
            if (
                not capture_policy
                or len(captured) != 1
                or not policy_denied(captured[0], "read")
            ):
                raise
            return {**snapshot, "content_sha256": None, "policy_reply": captured[0]}
        finally:
            session.transport = transport
        return {**snapshot, "content_sha256": digest(resource)}

    return grant.with_access_token(grant.owner_id, observe)


class AuthorityCampaign:
    """Concrete ordinary installed CLI execution; pending observations stay pending."""

    def __init__(
        self,
        config_path,
        *,
        binding,
        gateway_url,
        account,
        region,
        reader_role,
        client_factory,
    ):
        self.path = Path(config_path)
        self.config = validate_manifest(
            flow._decode(private_file(self.path, 65536)),
            binding=binding,
            gateway_url=gateway_url,
            account=account,
            region=region,
            reader_role=reader_role,
        )
        self.binding, self.gateway_url, self.client_factory = (
            binding,
            gateway_url,
            client_factory,
        )
        self.stack, self.runtime = ExitStack(), {}
        self.grants = {
            client: IdentityReadbackGrant(spec["grant"], spec["owner_id"])
            for client, spec in self.config["clients"].items()
        }

    def close(self):
        self.stack.close()

    def _start(self, client):
        if client not in self.runtime:
            runtime_path = self.path.parent / client / "runtime.json"
            recovery_id = None
            if runtime_path.exists():
                saved = flow._decode(private_file(runtime_path, 65536))
                if (
                    saved.get("binding") != self.binding
                    or saved.get("client") != client
                ):
                    raise ValueError(
                        "Original runtime belongs to another authority campaign"
                    )
                recovery_id = saved["recovery_id"]
                if not (runtime_path.parent / recovery_id).exists():
                    recovery_id = None
            resume = recovery_id is not None
            if not resume:
                recovery_id = str(uuid4())
                _write(
                    runtime_path,
                    {
                        "binding": self.binding,
                        "client": client,
                        "recovery_id": recovery_id,
                    },
                )
            installed, environ, hashes = self.stack.enter_context(
                (resume_installed_clients if resume else installed_clients)(
                    self.gateway_url,
                    self.binding["source_revision"],
                    recovery_root=self.path.parent / client,
                    recovery_id=recovery_id,
                )
            )
            import_session(
                installed, environ, self.path.parent / client / "session.token"
            )
            for name in (
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "OPENAI_BASE_URL",
                "ANTHROPIC_BASE_URL",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_PROFILE",
            ):
                environ.pop(name, None)
            configure_model_transport(installed, environ)
            self.runtime[client] = (installed, environ, hashes)
        return self.runtime[client]

    def _http(self, client, path):
        installed, environ, _ = self._start(client)
        result = run_bounded(
            [str(installed), "token"],
            environ=environ,
            timeout=30,
            max_output_bytes=16385,
        )
        token = result.stdout.decode().strip()
        if result.returncode or not token or any(c.isspace() for c in token):
            raise ValueError("Ordinary owner session unavailable")
        with httpx.Client(
            timeout=15, follow_redirects=False, trust_env=False
        ) as connection:
            response = connection.get(
                self.gateway_url + path, headers={"Authorization": "Bearer " + token}
            )
        if len(response.content) > 65536:
            raise ValueError("Owner-bound gateway observation unavailable")
        return response

    def _api(self, client, path):
        response = self._http(client, path)
        if response.status_code != 200:
            raise ValueError("Owner-bound gateway observation unavailable")
        return flow._decode(response.content)

    def _mcp(self, client, tool, arguments):
        installed, environ, _ = self._start(client)
        with self.client_factory([str(installed), "mcp"], environ=environ) as mcp:
            packet = mcp.request("tools/call", {"name": tool, "arguments": arguments})
        content = packet.get("content")
        if (
            type(content) is not list
            or len(content) != 1
            or content[0].get("type") != "text"
        ):
            raise ValueError("Original MCP receipt unavailable")
        return flow._decode(content[0]["text"])

    def _write_fixture(self, client, probe, *, resume=False):
        spec = self.config["clients"][client]
        _validate_owned_fixture(spec, probe, self.binding)
        owned = spec["owned_fixture"]
        record = owned["record"]
        provider = JiraFixtureProvider(
            owned["cleanup"],
            readback=spec["grant"],
            owner_id=spec["owner_id"],
            run_id=self.binding["run_id"],
        )
        provider.bind(record)
        provider.authorize()
        path = self.path.parent / client / "authority-owned-jira.json"
        if not path.exists():
            _write(path, record)
        ledger = JiraFixtureLedger(
            path,
            **{
                key: record[key]
                for key in (
                    "tenant_id",
                    "owner_id",
                    "run_id",
                    "site_id",
                    "project_key",
                    "operation_id",
                )
            },
        )
        current = ledger.state()
        if any(
            current.get(key) != record.get(key)
            for key in record
            if key not in {"cleanup_status", "operation_ids"}
        ):
            raise ValueError("Original fixture reservation changed")
        if not resume and (
            current != record
            or probe["provider_id"] not in ledger._observed(provider, current)
        ):
            raise ValueError(
                "Independent fixture ownership does not match the original reservation"
            )
        return ledger, provider

    def _prepare_foreign(self, row, path):
        probe, client = row["probe"], row["client"]
        subject = probe["subject_client"]
        principal = self._api(subject, "/api/auth/me")
        if (
            principal.get("user_id") != self.config["clients"][subject]["owner_id"]
            or principal.get("org_id") != self.config["tenant_id"]
        ):
            raise ValueError("Original operation owner is unavailable")
        observed = row["observations"]
        if "owner_receipt" not in observed:
            row["phase"] = "owner_invoking"
            _write(path, row)
            receipt = self._mcp(subject, probe["tool"], probe["arguments"])
            observed["owner_receipt"] = receipt
            row["phase"] = "owner_prepared"
            _write(path, row)
        elif observed["owner_receipt"].get("status") == "unknown":
            reference = observed["owner_receipt"].get("operation_ref")
            if (
                str(UUID(reference)) != reference
                or observed.get("owner_status_reads", 0) >= 3
            ):
                raise ValueError("Original owner recovery is unavailable or exhausted")
            observed["owner_status_reads"] = observed.get("owner_status_reads", 0) + 1
            _write(path, row)
            reconciled = self._mcp(
                subject, "adp___operation_status", {"operation_ref": reference}
            )
            if reconciled.get("operation_ref") != reference:
                raise ValueError(
                    "Owner recovery returned a different original operation"
                )
            observed["owner_receipt"] = reconciled
            _write(path, row)
        receipt = observed["owner_receipt"]
        if (
            receipt.get("status") != "confirmed"
            or receipt.get("result_status") != "success"
        ):
            raise ValueError("Original owner read has not completed")
        reference = str(UUID(receipt["operation_ref"]))
        execution = str(UUID(receipt["execution_id"]))
        if receipt.get("request_id") != reference:
            raise ValueError("Original receipt request does not match")
        _, owner_env, _ = self._start(subject)
        original = flow._decode(
            private_file(
                Path(owner_env["BG_CONFIG_DIR"])
                / "tool-operations"
                / (reference + ".json"),
                MAX_BYTES,
            )
        )
        if (
            any(
                original.get(key) != value
                for key, value in {
                    "request_id": reference,
                    "execution_id": execution,
                    "tool": probe["tool"],
                    "arguments": probe["arguments"],
                    "status": "confirmed",
                }.items()
            )
            or original.get("binding", {}).get("mode") != "bearer"
        ):
            raise ValueError("Original owned operation record is unavailable")
        route = f"/api/agent/tools/executions/{execution}/operations/{reference}?tool={quote(probe['tool'], safe='')}"
        owner_read = self._api(subject, route)
        if any(
            owner_read.get(key) != receipt.get(key)
            for key in (
                "request_id",
                "execution_id",
                "status",
                "result_status",
                "result",
            )
        ):
            raise ValueError("Owner receipt changed independently")
        denied = self._http(client, route)
        if denied.status_code not in {403, 404}:
            raise ValueError("Gateway did not deny another owner's receipt")
        _, acting_env, _ = self._start(client)
        directory = Path(acting_env["BG_CONFIG_DIR"]) / "tool-operations"
        directory.mkdir(mode=0o700, exist_ok=True)
        destination = directory / (reference + ".json")
        if (
            destination.exists()
            and flow._decode(private_file(destination, MAX_BYTES)) != original
        ):
            raise ValueError("Foreign receipt conflicts with an existing local record")
        _write(destination, original)
        observed.update(
            owner_principal=principal,
            owner_read=owner_read,
            foreign_receipt_status=denied.status_code,
            owner_operation=original,
        )
        row["native_probe"] = {
            "tool": "adp___operation_status",
            "arguments": {"operation_ref": reference},
        }
        _write(path, row)

    def _cleanup(self, row, path):
        observed, probe, client = row["observations"], row["probe"], row["client"]
        response = observed.get("native", {}).get("response", {})
        reference = response.get("operation_ref")
        if probe["tool"] == "atlassian___update_jira_issue":
            ledger, provider = self._write_fixture(client, probe, resume=True)
            if reference:
                ledger.track_operation(reference) if ledger.state()[
                    "cleanup_status"
                ] != "confirmed" else None
            original = self.config["clients"][client]["owned_fixture"]["record"]
            # Only the original confirmed create and this exact completed native
            # attempt are settled. Unknown IDs never acquire cleanup authority.
            settled = set(original["operation_ids"])
            if reference and response.get("status") in {"rejected", "confirmed"}:
                settled.add(reference)
            if not reference or response.get("status") not in {"rejected", "confirmed"}:
                raise ValueError(
                    "Write outcome is unresolved; retain the original fixture"
                )
            ledger.cleanup(
                provider,
                pending_operations=lambda operation_id: operation_id not in settled,
            )
            observed["fixture_cleanup"] = {
                "record": ledger.state(),
                "absence": provider.read_issue(
                    probe["provider_id"], site_id=probe["site_id"]
                ),
            }
            _write(path, row)
        if row["variant"] == "foreign_owner":
            close_response, close_client = (
                observed.get("owner_receipt", {}),
                probe["subject_client"],
            )
        else:
            close_response, close_client = response, client
        if close_response.get("status") == "confirmed" and close_response.get(
            "execution_id"
        ):
            if (
                observed.get("execution_cleanup", {}).get("state") != "closed"
                or observed.get("execution_cleanup", {}).get("cleanup_status")
                != "confirmed"
            ):
                observed["execution_cleanup"] = self._mcp(
                    close_client,
                    "adp___execution_close",
                    {"operation_ref": close_response["operation_ref"]},
                )
                _write(path, row)
            close = observed["execution_cleanup"]
            if (
                close.get("execution_id") != close_response["execution_id"]
                or close.get("state") != "closed"
                or close.get("cleanup_status") != "confirmed"
            ):
                raise ValueError("Original execution cleanup remains unconfirmed")
        elif response.get("status") != "rejected":
            raise ValueError("Original native outcome is unresolved")

    def _finish(self, row, path):
        observed, probe = row["observations"], row["probe"]
        try:
            if "after" not in observed:
                observed["after"] = provider_snapshot(
                    self.grants[probe["subject_client"]],
                    probe,
                    capture_policy=row["variant"] == "policy_denied",
                )
                _write(path, row)
        finally:
            # Settled owned effects can be cleaned even if a later read failed.
            # Unknown native operations remain retained for original recovery.
            self._cleanup(row, path)
        row["reason"] = "case-prerequisite-unverified"
        if row["variant"] in {"identity_refresh", "expired"}:
            row["reason"] = "supported-expiry-and-consent-history-unavailable"
        elif row["variant"] == "catalogue_schema":
            row["reason"] = "provider-catalogue-change-unobserved"
        else:
            qualify_denial(row, manifest=self.config)
            row.update(status="pass", reason=None, phase="verified")
        _write(path, row)
        return row

    def run(self, client, variant):
        if client not in CLIENTS or variant not in CASES:
            raise ValueError("Unknown authority case")
        lock = os.open(
            self.path.parent / client / "campaign.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._run(client, variant)
        finally:
            os.close(lock)

    def _run(self, client, variant):
        spec = self.config["clients"][client]
        probe = spec["probes"][variant]
        row = {
            "schema": "atlassian-authority-case/v1",
            "binding": self.binding,
            "client": client,
            "variant": variant,
            "status": "pending",
            "reason": "missing-provider-fixture",
            "probe": probe,
            "observations": None,
            "phase": "reserved",
        }
        if probe is None:
            return row
        path = self.path.parent / client / (variant + ".json")
        if path.exists():
            saved = flow._decode(private_file(path, MAX_BYTES))
            if any(
                saved.get(key) != row[key]
                for key in ("schema", "binding", "client", "variant", "probe")
            ):
                raise ValueError("Original authority attempt binding changed")
            row = saved
            if row["phase"] in {"invoking", "owner_invoking", "verified"}:
                return row
        row["reason"] = "original-attempt-unconfirmed"
        _write(path, row)
        try:
            if row["phase"] == "invoked":
                return self._finish(row, path)
            installed, environ, hashes = self._start(client)
            if row["phase"] != "owner_prepared":
                principal = self._api(client, "/api/auth/me")
                if (
                    principal.get("user_id") != spec["owner_id"]
                    or principal.get("org_id") != self.config["tenant_id"]
                ):
                    raise ValueError("Installed CLI belongs to another owner or tenant")
                connection = self._api(
                    client, "/api/auth/connections/atlassian-mcp/status"
                )
                access = self._api(client, "/api/auth/tools/access")
                if probe["tool"] == "atlassian___update_jira_issue":
                    self._write_fixture(client, probe)
                before = provider_snapshot(
                    self.grants[probe["subject_client"]],
                    probe,
                    capture_policy=variant == "policy_denied",
                )
                with self.client_factory(
                    [str(installed), "mcp"], environ=environ
                ) as mcp:
                    listing = mcp.request("tools/list")
                row["observations"] = {
                    "principal": principal,
                    "connection": connection,
                    "access": access,
                    "before": before,
                    "installed_hashes": hashes,
                    "listing": listing,
                }
                if variant == "policy_denied":
                    row["observations"]["policy"] = provider_snapshot(
                        self.grants[client], probe, capture_policy=True
                    )
                _write(path, row)
            if variant == "foreign_owner":
                self._prepare_foreign(row, path)
            row["phase"] = "invoking"
            _write(path, row)
            transcript, native = invoke_native(
                client, installed, environ, row.get("native_probe", probe), spec
            )
            row["phase"] = "invoked"
            row["observations"].update(transcript=transcript, native=native)
            _write(path, row)
            return self._finish(row, path)
        except (
            ValueError,
            KeyError,
            TypeError,
            OSError,
            TimeoutError,
            httpx.HTTPError,
        ):
            row["reason"] = "independent-authority-observation-unavailable"
        _write(path, row)
        return row


def qualify_denial(row, *, manifest):
    """A failed model, HTTP 401, timeout, unknown operation or flag alone cannot pass."""
    client, variant = row.get("client"), row.get("variant")
    if (
        client not in CLIENTS
        or variant not in DENIALS
        or row.get("binding") != manifest["binding"]
    ):
        raise ValueError("Bound authority denial required")
    spec, probe, observed = (
        manifest["clients"][client],
        row.get("probe"),
        row.get("observations"),
    )
    if (
        probe != spec["probes"][variant]
        or probe is None
        or type(observed) is not dict
        or observed.get("principal", {}).get("user_id") != spec["owner_id"]
        or observed.get("principal", {}).get("org_id") != manifest["tenant_id"]
    ):
        raise ValueError("Owner-bound native and independent observations required")
    native_probe = row.get("native_probe", probe)
    if variant == "foreign_owner":
        receipt = observed.get("owner_receipt", {})
        expected_probe = {
            "tool": "adp___operation_status",
            "arguments": {"operation_ref": receipt.get("operation_ref")},
        }
        if native_probe != expected_probe:
            raise ValueError("Original foreign operation reference required")
    elif native_probe != probe:
        raise ValueError("Native probe changed")
    native = native_observation(
        client,
        observed["transcript"],
        tool=native_probe["tool"],
        arguments=native_probe["arguments"],
        bounds=spec["bounds"],
    )
    response = native["response"]
    if (
        native != observed["native"]
        or str(UUID(response["operation_ref"])) != response["operation_ref"]
        or observed["before"] != observed["after"]
        or observed["before"].get("source") != "direct-atlassian-provider"
        or observed["before"].get("owner_id")
        != manifest["clients"][probe["subject_client"]]["owner_id"]
        or observed["before"].get("account_id")
        != manifest["clients"][probe["subject_client"]]["grant"]["provider_account_id"]
        or observed["before"].get("site_id") != probe["site_id"]
        or observed["before"].get("provider_id") != probe["provider_id"]
    ):
        raise ValueError(
            "Definitive native denial and unchanged independent provider content required"
        )
    if variant == "policy_denied":
        policy = observed.get("policy", {})
        close = observed.get("execution_cleanup", {})
        if (
            response.get("status") != "confirmed"
            or response.get("result_status") != "error"
            or not policy_denied(response.get("result"), "read")
            or not policy_denied(policy.get("policy_reply"), "read")
            or policy.get("content_sha256") is not None
            or policy.get("owner_id") != spec["owner_id"]
            or policy.get("account_id") != spec["grant"]["provider_account_id"]
            or policy.get("site_id") != probe["site_id"]
            or policy.get("provider_id") != probe["provider_id"]
            or not re.fullmatch(
                r"[0-9a-f]{64}", observed["before"].get("content_sha256") or ""
            )
            or close.get("execution_id") != response.get("execution_id")
            or close.get("state") != "closed"
            or close.get("cleanup_status") != "confirmed"
        ):
            raise ValueError(
                "Exact provider policy denial, independent content and cleanup required"
            )
        return {
            "client": client,
            "variant": variant,
            "status": "pass",
            "evidence_sha256": digest(row),
        }
    message = (
        OWNER_DENIAL
        if variant == "foreign_owner"
        else "Gateway rejected tool request (HTTP 403)"
    )
    if (
        response.get("status") != "rejected"
        or response.get("message") != message
        or set(response) != {"status", "message", "operation_ref", "recovery"}
        or not re.fullmatch(
            r"[0-9a-f]{64}", observed["before"].get("content_sha256") or ""
        )
    ):
        raise ValueError("Definitive denial and unchanged content are required")
    # Only conditions directly exposed by the current owner-bound APIs are
    # qualified here. Other variants retain their real attempt as pending.
    connection = observed["connection"].get("connection")
    if variant == "read_only":
        _validate_owned_fixture(spec, probe, manifest["binding"])
        access = observed.get("access", {})
        tools = access.get("tools")
        rows = (
            {item["name"]: item for item in tools}
            if type(tools) is list
            and all(
                type(item) is dict and type(item.get("name")) is str for item in tools
            )
            else {}
        )
        cleanup = observed.get("fixture_cleanup", {})
        record = cleanup.get("record", {})
        original = spec["owned_fixture"]["record"]
        if (
            access.get("user_id") != spec["owner_id"]
            or access.get("tenant_id") != manifest["tenant_id"]
            or rows.get("atlassian.update_jira_issue", {}).get("reason")
            != "not_allowed"
            or rows.get("atlassian.get_jira_issue", {}).get("ready") is not True
            or probe["tool"] != "atlassian___update_jira_issue"
            or cleanup.get("absence", object()) is not None
            or record
            != {
                **original,
                "operation_ids": list(
                    dict.fromkeys(
                        [*original["operation_ids"], response["operation_ref"]]
                    )
                ),
                "cleanup_status": "confirmed",
            }
        ):
            raise ValueError(
                "Read-only access and owned fixture cleanup are not independently confirmed"
            )
    elif variant == "foreign_owner":
        subject_spec = manifest["clients"][probe["subject_client"]]
        receipt, original = (
            observed.get("owner_read", {}),
            observed.get("owner_operation", {}),
        )
        close = observed.get("execution_cleanup", {})
        if (
            probe["subject_client"] == client
            or observed.get("owner_principal", {}).get("user_id")
            != subject_spec["owner_id"]
            or observed.get("owner_principal", {}).get("org_id")
            != manifest["tenant_id"]
            or observed.get("foreign_receipt_status") not in {403, 404}
            or receipt.get("status") != "confirmed"
            or receipt.get("result_status") != "success"
            or receipt.get("request_id") != response["operation_ref"]
            or original.get("request_id") != response["operation_ref"]
            or original.get("execution_id") != receipt.get("execution_id")
            or original.get("tool") != probe["tool"]
            or original.get("arguments") != probe["arguments"]
            or close.get("execution_id") != receipt.get("execution_id")
            or close.get("state") != "closed"
            or close.get("cleanup_status") != "confirmed"
        ):
            raise ValueError(
                "Original owner receipt, foreign denial and cleanup are required"
            )
    elif variant == "foreign_site":
        if (
            type(connection) is not dict
            or type(connection.get("sites")) is not list
            or probe["site_id"] in connection["sites"]
            or probe["site_id"]
            not in manifest["clients"][probe["subject_client"]]["grant"]["site_ids"]
            or probe["subject_client"] == client
        ):
            raise ValueError("Distinct independently granted foreign site required")
    elif variant == "disconnected":
        if (
            connection is not None
            or observed["connection"].get("status") != "consent_required"
        ):
            raise ValueError("Actual disconnected owner state required")
    else:
        raise ValueError(
            "The specific authority condition lacks an independent supported observation"
        )
    return {
        "client": client,
        "variant": variant,
        "status": "pass",
        "evidence_sha256": digest(row),
    }


def collect_authority(campaign):
    try:
        rows = [
            campaign.run(client, variant) for client in CLIENTS for variant in CASES
        ]
    finally:
        campaign.close()
    return {
        "schema": "atlassian-authority/v1",
        "binding": campaign.binding,
        "cases": rows,
        "status": "pass" if all(row["status"] == "pass" for row in rows) else "pending",
    }


def qualify_authority(report, *, manifest):
    """Replay supported assertions from retained observations, without effects.

    The caller supplies the protected original manifest. Status flags alone
    cannot qualify a row, and missing/duplicate cells invalidate the inventory.
    Expiry/history and catalogue-transition gaps remain explicit pending rows.
    """
    expected = {(client, variant) for client in CLIENTS for variant in CASES}
    if (
        type(report) is not dict
        or report.get("schema") != "atlassian-authority/v1"
        or report.get("binding") != manifest["binding"]
        or type(report.get("cases")) is not list
        or len(report["cases"]) != len(expected)
    ):
        raise ValueError("Complete bound authority inventory required")
    seen, results = set(), []
    for row in report["cases"]:
        key = row.get("client"), row.get("variant")
        if (
            key not in expected
            or key in seen
            or row.get("binding") != manifest["binding"]
        ):
            raise ValueError("Authority inventory is missing, duplicate or foreign")
        seen.add(key)
        if row.get("status") == "pass":
            results.append(qualify_denial(row, manifest=manifest))
        elif (
            row.get("status") == "pending"
            and type(row.get("reason")) is str
            and row["reason"]
        ):
            results.append(
                {
                    "client": key[0],
                    "variant": key[1],
                    "status": "pending",
                    "reason": row["reason"],
                }
            )
        else:
            raise ValueError("Authority outcome is unqualified")
    status = "pass" if all(row["status"] == "pass" for row in results) else "pending"
    if report.get("status") != status:
        raise ValueError("Authority aggregate differs from its qualified rows")
    return {
        "schema": "atlassian-authority-qualified/v1",
        "binding": manifest["binding"],
        "status": status,
        "cases": results,
    }
