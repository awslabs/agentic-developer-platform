"""Repository-owned legacy probes using installed clients and live Identity grants.

This collector reports observable read/post parity separately from the original
AC04 contract. Gateway receipts do not reveal provider HTTP request counts or
managed refresh history; those original predicates remain explicitly pending.
No imported operator plugin or supplied pass/counter can establish acceptance.
"""

from contextlib import ExitStack
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

import httpx

from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools.atlassian_authority_scenario import (
    BOUNDS,
    MAX_BYTES,
    _write,
    digest,
    invoke_native,
    native_observation,
)
from tests.e2e.tools.atlassian_regression_scenario import BASELINE, CLIENTS, _target
from tests.e2e.tools.atlassian_setup_observer import _aws
from tests.e2e.tools.client_matrix import import_session
from tests.e2e.tools.installed_clients import (
    installed_clients,
    resume_installed_clients,
    configure_model_transport,
)
from tests.e2e.tools.process_bounds import run_bounded
from modules.tools.platform.scripts.native_test_session import private_file

CONNECTORS = ("github", "jira", "slack")
CELLS = tuple(f"{connector}:{client}" for connector in CONNECTORS for client in CLIENTS)
HANDLE = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z")
GRANT_FIELDS = {
    "account",
    "region",
    "environment",
    "adapter_role_arn",
    "adapter_label",
    "workload_name",
    "provider_name",
    "scopes",
    "provider_account_id",
}
SCOPES = {
    "github": {"read:user"},
    "jira": {"read:jira-work", "read:me", "offline_access"},
    "slack": {"users:read", "chat:write", "channels:history", "groups:history"},
}


def validate_manifest(
    value, *, binding, target, gateway_url, account, region, reader_role
):
    flow.validate_binding(binding)
    _target(target)
    if (
        type(value) is not dict
        or set(value) != {"binding", "target", "gateway_url", "tenant_id", "cells"}
        or value["binding"] != binding
        or value["target"] != target
        or value["gateway_url"] != gateway_url
        or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::443)?", gateway_url)
        or not isinstance(value["tenant_id"], str)
        or not HANDLE.fullmatch(value["tenant_id"])
        or type(value["cells"]) is not dict
        or set(value["cells"]) != set(CELLS)
    ):
        raise ValueError("Complete protected legacy release binding required")
    for cell, spec in value["cells"].items():
        if spec is None:
            continue
        connector, client = cell.split(":")
        if (
            type(spec) is not dict
            or set(spec) != {"owner_id", "grant", "fixture", "model", "bounds"}
            or not isinstance(spec["owner_id"], str)
            or not HANDLE.fullmatch(spec["owner_id"])
            or not isinstance(spec["model"], str)
            or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", spec["model"])
            or type(spec["bounds"]) is not dict
            or set(spec["bounds"]) != set(BOUNDS)
            or any(
                type(spec["bounds"][key]) is not int
                or not 0 < spec["bounds"][key] <= limit
                for key, limit in BOUNDS.items()
            )
        ):
            raise ValueError("Bounded legacy client and owner required")
        grant = spec["grant"]
        if (
            type(grant) is not dict
            or set(grant) != GRANT_FIELDS
            or grant["account"] != account
            or grant["region"] != region
            or grant["adapter_role_arn"] != reader_role
            or not re.fullmatch(
                rf"arn:aws:iam::{re.escape(account)}:role/[A-Za-z0-9+=,.@_/-]+",
                reader_role,
            )
            or grant["adapter_label"] != "oidc-adapter"
            or not all(
                isinstance(grant[key], str) and HANDLE.fullmatch(grant[key])
                for key in (
                    "environment",
                    "workload_name",
                    "provider_name",
                    "provider_account_id",
                )
            )
            or not grant["workload_name"].endswith("-oauth-user")
            or grant["provider_name"]
            != grant["workload_name"].removesuffix("-oauth-user")
            + "-"
            + ("atlassian" if connector == "jira" else connector)
            + "-user"
            or type(grant["scopes"]) is not list
            or len(set(grant["scopes"])) != len(grant["scopes"])
            or set(grant["scopes"]) - SCOPES[connector]
        ):
            raise ValueError("Selected legacy Identity reader grant required")
        fixture = spec["fixture"]
        expected = {
            "github": set(),
            "jira": {"site_id", "issue_id", "issue_key", "summary"},
            "slack": {
                "team_id",
                "user_id",
                "channel_id",
                "message",
                "run_marker",
                "cleanup_authorized",
            },
        }[connector]
        if type(fixture) is not dict or set(fixture) != expected:
            raise ValueError("Bounded existing provider fixture required")
        if connector == "jira" and (
            not {"read:jira-work", "read:me"} <= set(grant["scopes"])
            or not all(
                isinstance(fixture[key], str) and HANDLE.fullmatch(fixture[key])
                for key in ("site_id", "issue_id", "issue_key")
            )
            or not isinstance(fixture["summary"], str)
            or not 0 < len(fixture["summary"]) <= 512
        ):
            raise ValueError("Existing Jira issue and REST grant required")
        if connector == "slack" and (
            fixture["cleanup_authorized"] is not True
            or not {"users:read", "chat:write"} <= set(grant["scopes"])
            or not {"channels:history", "groups:history"} & set(grant["scopes"])
            or not all(
                isinstance(fixture[key], str) and re.fullmatch(pattern, fixture[key])
                for key, pattern in (
                    ("team_id", r"T[A-Z0-9]{8,63}"),
                    ("user_id", r"[UW][A-Z0-9]{8,63}"),
                    ("channel_id", r"[CG][A-Z0-9]{8,63}"),
                )
            )
            or fixture["user_id"] != grant["provider_account_id"]
            or not isinstance(fixture["message"], str)
            or not 0 < len(fixture["message"]) <= 180
            or any(char in fixture["message"] for char in "@<>\r\n\x00")
            or not isinstance(fixture["run_marker"], str)
            or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                fixture["run_marker"],
            )
        ):
            raise ValueError(
                "Owned Slack test message and separately consented read/delete capability required"
            )
    if len(flow.canonical(value)) > 65536:
        raise ValueError("Legacy binding exceeds its bound")
    return deepcopy(value)


def bind_legacy(raw, sessions, *, directory, **binding):
    if len(raw.encode()) > 65536 or len(sessions.encode()) > 160000:
        raise ValueError("Legacy protected input exceeds bound")
    value = validate_manifest(flow._decode(raw), **binding)
    tokens = flow._decode(sessions)
    expected = {key for key, spec in value["cells"].items() if spec is not None}
    if (
        type(tokens) is not dict
        or set(tokens) != expected
        or any(
            type(token) is not str
            or not 0 < len(token) <= 16384
            or any(c.isspace() for c in token)
            for token in tokens.values()
        )
    ):
        raise ValueError("Ordinary owner sessions for the selected cells required")
    root = Path(tempfile.mkdtemp(prefix="legacy-live-", dir=directory))
    try:
        for key, token in tokens.items():
            cell = root / key.replace(":", "-")
            cell.mkdir(mode=0o700)
            with os.fdopen(
                os.open(
                    cell / "session.token", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                ),
                "w",
            ) as stream:
                stream.write(token)
        _write(root / "config.json", value)
        return root / "config.json"
    except Exception:
        shutil.rmtree(root)
        raise


class LegacyProvider:
    """Fixed provider endpoints through an existing owner-bound Identity grant."""

    def __init__(self, connector, spec):
        self.connector, self.spec, self.config = connector, spec, spec["grant"]

    def with_token(self, callback):
        workload = _aws(
            self.config,
            "adapter",
            "bedrock-agentcore",
            "get-workload-access-token-for-user-id",
            input_value={
                "workloadName": self.config["workload_name"],
                "userId": self.spec["owner_id"],
            },
        )
        token = workload.get("workloadAccessToken")
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 32768
            or any(c.isspace() for c in token)
        ):
            raise ValueError("Original Identity workload unavailable")
        result = _aws(
            self.config,
            "adapter",
            "bedrock-agentcore",
            "get-resource-oauth2-token",
            input_value={
                "workloadIdentityToken": token,
                "resourceCredentialProviderName": self.config["provider_name"],
                "scopes": self.config["scopes"],
                "oauth2Flow": "USER_FEDERATION",
            },
        )
        token = result.get("accessToken")
        if (
            result.get("sessionStatus") in {"FAILED", "IN_PROGRESS"}
            or not isinstance(token, str)
            or not 0 < len(token) <= 16384
            or any(c.isspace() for c in token)
        ):
            raise ValueError("Existing owner consent is unavailable")
        return callback(token)

    @staticmethod
    def request(token, origin, path, *, parameters=None, body=None):
        # Callers select only constant provider origins; query/body is structured.
        if origin not in {
            "https://api.github.com",
            "https://api.atlassian.com",
            "https://slack.com",
        }:
            raise ValueError("Unsupported provider origin")
        with httpx.Client(
            timeout=15, follow_redirects=False, trust_env=False
        ) as client:
            with client.stream(
                "POST" if body is not None else "GET",
                origin + path,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                },
                params=parameters,
                json=body,
            ) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise ValueError("Independent provider response exceeds bound")
                if response.status_code != 200:
                    raise ValueError("Independent provider read or cleanup unavailable")
                return flow._decode(bytes(data)), dict(response.headers)

    def _identity(self, token):
        fixture = self.spec["fixture"]
        if self.connector == "github":
            user, headers = self.request(token, "https://api.github.com", "/user")
            if (
                type(user.get("id")) is not int
                or str(user["id"]) != self.config["provider_account_id"]
            ):
                raise ValueError("GitHub provider account changed")
            scopes = sorted(
                s.strip()
                for s in headers.get("x-oauth-scopes", "").split(",")
                if s.strip()
            )
            return {"account_id": str(user["id"]), "scopes": scopes}, user
        if self.connector == "jira":
            user, _ = self.request(token, "https://api.atlassian.com", "/me")
            sites, _ = self.request(
                token, "https://api.atlassian.com", "/oauth/token/accessible-resources"
            )
            matches = (
                [
                    site
                    for site in sites
                    if type(site) is dict and site.get("id") == fixture["site_id"]
                ]
                if type(sites) is list
                else []
            )
            if (
                user.get("account_id") != self.config["provider_account_id"]
                or len(matches) != 1
                or "read:jira-work" not in matches[0].get("scopes", [])
            ):
                raise ValueError("Jira account/site authority changed")
            return {
                "account_id": user["account_id"],
                "site_id": fixture["site_id"],
                "scopes": sorted(matches[0]["scopes"]),
            }, None
        identity, headers = self.request(token, "https://slack.com", "/api/auth.test")
        scopes = sorted(
            s.strip() for s in headers.get("x-oauth-scopes", "").split(",") if s.strip()
        )
        if (
            identity.get("ok") is not True
            or identity.get("user_id") != self.config["provider_account_id"]
            or identity.get("team_id") != fixture["team_id"]
            or not {"users:read", "chat:write"} <= set(scopes)
        ):
            raise ValueError("Slack owner/team/scope authority changed")
        return {
            "account_id": identity["user_id"],
            "team_id": identity["team_id"],
            "scopes": scopes,
        }, None

    def snapshot(self):
        def read(token):
            identity, profile = self._identity(token)
            fixture = self.spec["fixture"]
            if self.connector == "jira":
                value, _ = self.request(
                    token,
                    "https://api.atlassian.com",
                    "/ex/jira/"
                    + quote(fixture["site_id"], safe="")
                    + "/rest/api/3/issue/"
                    + quote(fixture["issue_id"], safe=""),
                    parameters={"fields": "summary"},
                )
                profile = {
                    "id": value.get("id"),
                    "key": value.get("key"),
                    "fields": {"summary": value.get("fields", {}).get("summary")},
                }
                if profile != {
                    "id": fixture["issue_id"],
                    "key": fixture["issue_key"],
                    "fields": {"summary": fixture["summary"]},
                }:
                    raise ValueError("Independent Jira fixture changed")
            elif self.connector == "slack":
                value, _ = self.request(
                    token,
                    "https://slack.com",
                    "/api/users.info",
                    parameters={"user": fixture["user_id"]},
                )
                profile = value.get("user")
                if (
                    value.get("ok") is not True
                    or type(profile) is not dict
                    or profile.get("id") != fixture["user_id"]
                ):
                    raise ValueError("Independent Slack user unavailable")
            return {
                "source": "direct-legacy-provider",
                "connector": self.connector,
                "owner_id": self.spec["owner_id"],
                "identity": identity,
                "profile": profile,
            }

        return self.with_token(read)

    def messages(self, marker, *, oldest):
        def read(token):
            self._identity(token)
            fixture = self.spec["fixture"]
            result, _ = self.request(
                token,
                "https://slack.com",
                "/api/conversations.history",
                parameters={
                    "channel": fixture["channel_id"],
                    "oldest": oldest,
                    "inclusive": "true",
                    "limit": 100,
                },
            )
            if (
                result.get("ok") is not True
                or result.get("has_more") is True
                or result.get("response_metadata", {}).get("next_cursor")
                or type(result.get("messages")) is not list
            ):
                raise ValueError("Complete bounded Slack history unavailable")
            rows = []
            for message in result["messages"]:
                if marker not in str(message.get("text", "")):
                    continue
                if (
                    message.get("text") != fixture["message"] + f" [run:{marker}]"
                    or message.get("user") != fixture["user_id"]
                    or not isinstance(message.get("ts"), str)
                    or not re.fullmatch(r"[0-9]{10,16}\.[0-9]{1,6}", message["ts"])
                ):
                    raise ValueError("Slack marker collision or foreign message")
                rows.append(
                    {
                        "channel": fixture["channel_id"],
                        "user": fixture["user_id"],
                        "text": message["text"],
                        "ts": message["ts"],
                    }
                )
            if len(rows) > 1:
                raise ValueError("Ambiguous Slack write; retain original operation")
            return rows

        return self.with_token(read)

    def delete(self, marker, ts, *, oldest):
        fixture = self.spec["fixture"]
        if fixture["cleanup_authorized"] is not True or self.messages(
            marker, oldest=oldest
        ) != [
            {
                "channel": fixture["channel_id"],
                "user": fixture["user_id"],
                "text": fixture["message"] + f" [run:{marker}]",
                "ts": ts,
            }
        ]:
            raise ValueError(
                "Only the exact independently observed owned message may be deleted"
            )

        def remove(token):
            self._identity(token)
            reply, _ = self.request(
                token,
                "https://slack.com",
                "/api/chat.delete",
                body={"channel": fixture["channel_id"], "ts": ts},
            )
            if (
                reply.get("ok") is not True
                or reply.get("channel") != fixture["channel_id"]
                or reply.get("ts") != ts
            ):
                raise ValueError("Slack deletion remains unconfirmed")

        return self.with_token(remove)


def provider_result(receipt):
    if (
        receipt.get("status") != "confirmed"
        or receipt.get("result_status") != "success"
    ):
        raise ValueError("Original provider operation is not confirmed successful")
    value = receipt.get("result")
    for _ in range(5):
        if type(value) is not dict or value.get("isError") is True or "error" in value:
            raise ValueError("Provider result is unavailable")
        if "result" in value:
            value = value["result"]
        elif "content" in value:
            content = value["content"]
            if (
                type(content) is not list
                or len(content) != 1
                or content[0].get("type") != "text"
            ):
                raise ValueError("One provider JSON result required")
            value = flow._decode(content[0]["text"])
        else:
            return value
    raise ValueError("Provider result nesting exceeds bound")


def projection(connector, value):
    if type(value) is not dict:
        raise ValueError("Provider object required")
    if connector == "github":
        if (
            type(value.get("id")) is not int
            or not isinstance(value.get("login"), str)
            or not value["login"]
        ):
            raise ValueError("Actual GitHub profile required")
        return {key: value[key] for key in ("id", "login")}
    if connector == "slack":
        if not all(
            isinstance(value.get(key), str) and value[key] for key in ("id", "name")
        ):
            raise ValueError("Actual Slack profile required")
        return {key: value[key] for key in ("id", "name")}
    return {
        "id": value.get("id"),
        "key": value.get("key"),
        "fields": {"summary": value.get("fields", {}).get("summary")},
    }


class LegacyCell:
    def __init__(self, root, cell, manifest, *, client_factory):
        self.root, self.cell, self.manifest = Path(root), cell, manifest
        self.connector, self.client = cell.split(":")
        self.spec = manifest["cells"][cell]
        self.provider = LegacyProvider(self.connector, self.spec)
        self.client_factory, self.stack, self.runtime = (
            client_factory,
            ExitStack(),
            None,
        )
        self.path = self.root / "journal.json"

    def close(self):
        self.stack.close()

    def start(self):
        if self.runtime is None:
            record = self.root / "runtime.json"
            previous = (
                flow._decode(private_file(record, 65536)) if record.exists() else None
            )
            if previous and (
                previous.get("binding") != self.manifest["binding"]
                or previous.get("cell") != self.cell
            ):
                raise ValueError("Legacy runtime binding changed")
            identifier = previous["id"] if previous else str(uuid4())
            if str(UUID(identifier)) != identifier:
                raise ValueError("Original runtime identifier is invalid")
            resume = previous is not None
            if resume and not (self.root / identifier).is_dir():
                raise ValueError("Original runtime directory unavailable")
            _write(
                record,
                {
                    "binding": self.manifest["binding"],
                    "cell": self.cell,
                    "id": identifier,
                },
            )
            self.runtime = self.stack.enter_context(
                (resume_installed_clients if resume else installed_clients)(
                    self.manifest["gateway_url"],
                    self.manifest["target"]["gateway"],
                    recovery_root=self.root,
                    recovery_id=identifier,
                )
            )
            installed, environ, _ = self.runtime
            import_session(installed, environ, self.root / "session.token")
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
            if self.client != "cli":
                configure_model_transport(installed, environ)
        return self.runtime

    def api(self, path):
        installed, environ, _ = self.start()
        result = run_bounded(
            [str(installed), "token"],
            environ=environ,
            timeout=30,
            max_output_bytes=16385,
        )
        token = result.stdout.decode().strip()
        if (
            result.returncode
            or not token
            or len(token) > 16384
            or any(c.isspace() for c in token)
        ):
            raise ValueError("Original ADP owner session unavailable")
        with httpx.Client(
            timeout=15, trust_env=False, follow_redirects=False
        ) as client:
            response = client.get(
                self.manifest["gateway_url"] + path,
                headers={"Authorization": "Bearer " + token},
            )
        if response.status_code != 200 or len(response.content) > MAX_BYTES:
            raise ValueError("Independent owner receipt unavailable")
        return flow._decode(response.content)

    def mcp(self, method, parameters=None):
        installed, environ, _ = self.start()
        with self.client_factory([str(installed), "mcp"], environ=environ) as client:
            return client.request(method, parameters)

    def lifecycle(self, tool, reference):
        packet = self.mcp(
            "tools/call", {"name": tool, "arguments": {"operation_ref": reference}}
        )
        content = packet.get("content")
        if (
            type(content) is not list
            or len(content) != 1
            or content[0].get("type") != "text"
        ):
            raise ValueError("Original lifecycle receipt unavailable")
        return flow._decode(content[0]["text"])

    def _saved_operations(self):
        _, environ, _ = self.start()
        directory = Path(environ["BG_CONFIG_DIR"]) / "tool-operations"
        rows = {}
        if directory.exists():
            for path in directory.glob("*.json"):
                if len(rows) >= 16:
                    raise ValueError("Legacy operation inventory exceeds bound")
                rows[path.stem] = flow._decode(private_file(path, MAX_BYTES))
        return rows

    def call_once(self, journal, name, tool, arguments):
        steps = journal["steps"]
        if name in steps:
            step = steps[name]
            if step.get("tool") != tool or step.get("arguments") != arguments:
                raise ValueError("Original legacy operation changed")
            if step["phase"] == "invoking":
                original = self._saved_operations()
                new = {
                    key: value
                    for key, value in original.items()
                    if key not in step["prior_operations"]
                }
                if len(new) != 1:
                    raise ValueError(
                        "Original operation reference cannot be uniquely recovered"
                    )
                reference, saved = next(iter(new.items()))
                if saved.get("tool") != tool or saved.get("arguments") != arguments:
                    raise ValueError("Original native operation does not match")
                step["receipt"] = self.lifecycle("adp___operation_status", reference)
                step["phase"] = "reconciled"
                # The original receipt permits cleanup; missing native transcript
                # still prevents a claim that the intended model invocation passed.
                _write(self.path, journal)
            return step
        step = {
            "tool": tool,
            "arguments": arguments,
            "phase": "invoking",
            "prior_operations": sorted(self._saved_operations()),
        }
        steps[name] = step
        _write(self.path, journal)
        installed, environ, hashes = self.start()
        if self.client == "cli":
            packet = self.mcp("tools/call", {"name": tool, "arguments": arguments})
            content = packet.get("content")
            if (
                type(content) is not list
                or len(content) != 1
                or content[0].get("type") != "text"
            ):
                raise ValueError("Original CLI receipt unavailable")
            step.update(
                packet=packet,
                receipt=flow._decode(content[0]["text"]),
                installed_hashes=hashes,
            )
        else:
            raw, native = invoke_native(
                self.client,
                installed,
                environ,
                {"tool": tool, "arguments": arguments},
                self.spec,
            )
            step.update(
                transcript=raw,
                native=native,
                receipt=native["response"],
                installed_hashes=hashes,
            )
        step["phase"] = "invoked"
        _write(self.path, journal)
        return step

    def read_receipt(self, step):
        receipt = step["receipt"]
        request = str(UUID(receipt["operation_ref"]))
        execution = str(UUID(receipt["execution_id"]))
        if receipt.get("request_id") != request:
            raise ValueError("Original execution request changed")
        actual = self.api(
            f"/api/agent/tools/executions/{execution}/operations/{request}?"
            + urlencode({"tool": step["tool"]})
        )
        if any(
            actual.get(key) != receipt.get(key)
            for key in (
                "status",
                "result_status",
                "request_id",
                "execution_id",
                "result",
            )
        ):
            raise ValueError("Independent gateway receipt differs")
        step["independent_receipt"] = actual

    def cleanup(self, journal):
        errors = []
        if self.connector == "slack" and "post" in journal["steps"]:
            try:
                step = journal["steps"]["post"]
                if step.get("receipt", {}).get("status") != "confirmed":
                    raise ValueError("Original Slack post is unresolved")
                posted = provider_result(step["receipt"])
                fixture, marker = self.spec["fixture"], journal["marker"]
                expected = {
                    "channel_id": fixture["channel_id"],
                    "user_id": fixture["user_id"],
                    "text": fixture["message"] + f" [run:{marker}]",
                    "run_marker": marker,
                }
                if any(posted.get(key) != value for key, value in expected.items()):
                    raise ValueError("Original Slack result is unqualified")
                messages = self.provider.messages(marker, oldest=journal["oldest"])
                if journal.get("delete_phase") not in {"invoking", "confirmed"}:
                    if messages != [
                        {
                            "channel": fixture["channel_id"],
                            "user": fixture["user_id"],
                            "text": expected["text"],
                            "ts": posted.get("ts"),
                        }
                    ]:
                        raise ValueError("Independent original post is unavailable")
                    journal["delete_phase"] = "invoking"
                    _write(self.path, journal)
                    self.provider.delete(marker, posted["ts"], oldest=journal["oldest"])
                remaining = self.provider.messages(marker, oldest=journal["oldest"])
                if remaining:
                    raise ValueError(
                        "Original deletion remains unconfirmed; do not replay"
                    )
                journal.update(delete_phase="confirmed", owned_messages_after=[])
                _write(self.path, journal)
            except (ValueError, KeyError, OSError, httpx.HTTPError):
                errors.append("original-message-cleanup-unconfirmed")
        for step in journal["steps"].values():
            receipt = step.get("receipt", {})
            if receipt.get("status") == "rejected":
                continue
            if receipt.get("status") != "confirmed" or not receipt.get("execution_id"):
                errors.append("original-operation-unresolved")
                continue
            try:
                close = step.get("close", {})
                if (
                    close.get("state") != "closed"
                    or close.get("cleanup_status") != "confirmed"
                ):
                    close = self.lifecycle(
                        "adp___execution_close", receipt["operation_ref"]
                    )
                    step["close"] = close
                    _write(self.path, journal)
                if (
                    close.get("execution_id") != receipt["execution_id"]
                    or close.get("state") != "closed"
                    or close.get("cleanup_status") != "confirmed"
                ):
                    raise ValueError("Original execution cleanup unavailable")
            except (ValueError, KeyError, OSError, httpx.HTTPError):
                errors.append("original-execution-cleanup-unconfirmed")
        journal["cleanup"] = "pending" if errors else "confirmed"
        _write(self.path, journal)
        if errors:
            raise ValueError(errors[0])

    def run(self):
        import time

        lock = os.open(
            self.root / "campaign.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._run(time)
        finally:
            os.close(lock)

    def _run(self, time):
        expected = {
            "schema": "legacy-live-cell/v1",
            "binding": self.manifest["binding"],
            "target": self.manifest["target"],
            "cell": self.cell,
        }
        if self.path.exists():
            journal = flow._decode(private_file(self.path, MAX_BYTES))
            if any(journal.get(key) != value for key, value in expected.items()):
                raise ValueError("Original legacy journal binding changed")
        else:
            journal = {
                **expected,
                "marker": (
                    self.spec["fixture"]["run_marker"]
                    if self.connector == "slack"
                    else str(uuid4())
                ),
                "oldest": str(max(0, int(time.time()) - 30)),
                "started_at": int(time.time()),
                "steps": {},
                "status": "pending",
                "reason": "unverified",
                "cleanup": "pending",
            }
            _write(self.path, journal)
        if journal["status"] == "pass":
            return journal
        try:
            if "before" not in journal:
                principal = self.api("/api/auth/me")
                if (
                    principal.get("user_id") != self.spec["owner_id"]
                    or principal.get("org_id") != self.manifest["tenant_id"]
                ):
                    raise ValueError("Legacy session owner or tenant differs")
                before = self.provider.snapshot()
                listing = self.mcp("tools/list")
                validate_catalogue(listing, self.connector)
                journal.update(principal=principal, before=before, listing=listing)
                if self.connector == "slack":
                    journal["owned_messages_before"] = self.provider.messages(
                        journal["marker"], oldest=journal["oldest"]
                    )
                    if journal["owned_messages_before"]:
                        raise ValueError("Slack run marker is not fresh")
                _write(self.path, journal)
            fixture = self.spec["fixture"]
            tool = self.connector + (
                "___get_issue" if self.connector == "jira" else "___get_user"
            )
            arguments = (
                {"cloudId": fixture["site_id"], "issueIdOrKey": fixture["issue_id"]}
                if self.connector == "jira"
                else {"user": fixture["user_id"]}
                if self.connector == "slack"
                else {}
            )
            if (
                "read" not in journal["steps"]
                and time.time() - journal["started_at"] > 300
            ):
                raise ValueError("Original session deadline expired before dispatch")
            step = self.call_once(journal, "read", tool, arguments)
            self.read_receipt(step)
            if projection(
                self.connector, provider_result(step["receipt"])
            ) != projection(self.connector, journal["before"]["profile"]):
                raise ValueError("Installed client differs from the actual provider")
            if "after" not in journal:
                journal["after"] = self.provider.snapshot()
                _write(self.path, journal)
            if self.connector == "slack":
                if (
                    "post" not in journal["steps"]
                    and time.time() - journal["started_at"] > 300
                ):
                    raise ValueError("Original session deadline expired before post")
                post = self.call_once(
                    journal,
                    "post",
                    "slack___post_message",
                    {
                        "channel": fixture["channel_id"],
                        "text": fixture["message"] + f" [run:{journal['marker']}]",
                        "run_marker": journal["marker"],
                    },
                )
                self.read_receipt(post)
                if "posted_messages" not in journal:
                    journal["posted_messages"] = self.provider.messages(
                        journal["marker"], oldest=journal["oldest"]
                    )
                    _write(self.path, journal)
        except (
            ValueError,
            KeyError,
            TypeError,
            OSError,
            TimeoutError,
            httpx.HTTPError,
        ):
            journal["reason"] = "original-client-provider-observation-unavailable"
        finally:
            try:
                self.cleanup(journal)
            except (
                ValueError,
                KeyError,
                TypeError,
                OSError,
                TimeoutError,
                httpx.HTTPError,
            ):
                journal["reason"] = "original-cleanup-unconfirmed"
        journal["completed_at"] = int(time.time())
        try:
            qualify_cell(journal, manifest=self.manifest)
            journal.update(status="pass", reason=None)
        except (ValueError, KeyError, TypeError):
            pass
        _write(self.path, journal)
        return journal


def validate_catalogue(listing, connector):
    tools = listing.get("tools") if type(listing) is dict else None
    if type(tools) is not list or any(
        type(tool) is not dict or not isinstance(tool.get("name"), str)
        for tool in tools
    ):
        raise ValueError("Deployed legacy catalogue unavailable")
    names = [tool["name"] for tool in tools]
    if len(set(names)) != len(names):
        raise ValueError("Ambiguous deployed catalogue")
    expected = {
        key: item["inputSchema"]
        for key, item in BASELINE.items()
        if key.startswith(connector + "___")
    }
    actual = {
        tool["name"]: tool.get("inputSchema")
        for tool in tools
        if tool["name"].startswith(connector + "___")
    }
    if actual != expected:
        raise ValueError("Legacy catalogue differs from the accepted baseline")
    return actual


def qualify_cell(row, *, manifest):
    """Recheck actual native/API/provider observations; never certify old counters."""
    cell = row.get("cell")
    if (
        cell not in CELLS
        or manifest["cells"][cell] is None
        or row.get("schema") != "legacy-live-cell/v1"
        or row.get("binding") != manifest["binding"]
        or row.get("target") != manifest["target"]
    ):
        raise ValueError("Protected original legacy cell required")
    connector, client = cell.split(":")
    spec, steps = manifest["cells"][cell], row.get("steps", {})
    if (
        set(steps) != ({"read", "post"} if connector == "slack" else {"read"})
        or row.get("cleanup") != "confirmed"
    ):
        raise ValueError("Complete original legacy operations and cleanup required")
    principal = row.get("principal", {})
    if (
        principal.get("user_id") != spec["owner_id"]
        or principal.get("org_id") != manifest["tenant_id"]
    ):
        raise ValueError("Original ADP principal changed")
    validate_catalogue(row["listing"], connector)
    before, after = row["before"], row["after"]
    if (
        before.get("source") != "direct-legacy-provider"
        or before.get("connector") != connector
        or before.get("owner_id") != spec["owner_id"]
        or before.get("identity", {}).get("account_id")
        != spec["grant"]["provider_account_id"]
        or before != after
    ):
        raise ValueError("Independent original provider identity/content changed")
    required_scopes = {
        item["scope"]
        for key, item in BASELINE.items()
        if key.startswith(connector + "___")
    }
    if not required_scopes <= set(before["identity"].get("scopes", [])):
        raise ValueError("Independent legacy scope observation unavailable")
    fixture = spec["fixture"]
    read_arguments = (
        {"cloudId": fixture["site_id"], "issueIdOrKey": fixture["issue_id"]}
        if connector == "jira"
        else {"user": fixture["user_id"]}
        if connector == "slack"
        else {}
    )
    expected = {
        "read": (
            connector + ("___get_issue" if connector == "jira" else "___get_user"),
            read_arguments,
        )
    }
    if connector == "slack":
        if (
            str(UUID(row["marker"])) != row["marker"]
            or row["marker"] != fixture["run_marker"]
        ):
            raise ValueError("Original Slack marker required")
        expected["post"] = (
            "slack___post_message",
            {
                "channel": fixture["channel_id"],
                "text": fixture["message"] + f" [run:{row['marker']}]",
                "run_marker": row["marker"],
            },
        )
    for name, step in steps.items():
        tool, arguments = expected[name]
        if (
            step.get("tool") != tool
            or step.get("arguments") != arguments
            or step.get("phase") != "invoked"
        ):
            raise ValueError("Original installed invocation is unconfirmed")
        if client != "cli":
            native = native_observation(
                client,
                step["transcript"],
                tool=tool,
                arguments=arguments,
                bounds=spec["bounds"],
            )
            if native != step.get("native") or native["response"] != step.get(
                "receipt"
            ):
                raise ValueError("Native invocation/receipt changed")
        else:
            packet = step.get("packet", {})
            content = packet.get("content")
            if (
                type(content) is not list
                or len(content) != 1
                or content[0].get("type") != "text"
                or flow._decode(content[0]["text"]) != step.get("receipt")
            ):
                raise ValueError("Original CLI receipt changed")
        receipt = step["receipt"]
        actual = step.get("independent_receipt", {})
        if any(
            actual.get(key) != receipt.get(key)
            for key in (
                "status",
                "result_status",
                "request_id",
                "execution_id",
                "result",
            )
        ):
            raise ValueError("Independent original receipt changed")
        if (
            any(
                str(UUID(receipt[key])) != receipt[key]
                for key in ("request_id", "execution_id", "operation_ref")
            )
            or receipt["request_id"] != receipt["operation_ref"]
        ):
            raise ValueError("Original execution/request binding changed")
        close = step.get("close", {})
        if (
            close.get("execution_id") != receipt["execution_id"]
            or close.get("state") != "closed"
            or close.get("cleanup_status") != "confirmed"
        ):
            raise ValueError("Original execution cleanup unconfirmed")
        if name == "read" and projection(
            connector, provider_result(receipt)
        ) != projection(connector, before["profile"]):
            raise ValueError("Client result differs from independent provider")
    if connector == "slack":
        post = provider_result(steps["post"]["receipt"])
        values = {
            "channel_id": fixture["channel_id"],
            "user_id": fixture["user_id"],
            "text": expected["post"][1]["text"],
            "run_marker": row["marker"],
        }
        if (
            any(post.get(key) != value for key, value in values.items())
            or row.get("owned_messages_before") != []
            or row.get("posted_messages")
            != [
                {
                    "channel": fixture["channel_id"],
                    "user": fixture["user_id"],
                    "text": values["text"],
                    "ts": post.get("ts"),
                }
            ]
            or not isinstance(post.get("ts"), str)
            or not re.fullmatch(r"[0-9]{10,16}\.[0-9]{1,6}", post["ts"])
            or row.get("delete_phase") != "confirmed"
            or row.get("owned_messages_after") != []
        ):
            raise ValueError(
                "Exact Slack post and independent deletion are unconfirmed"
            )
    if (
        type(row.get("started_at")) is not int
        or type(row.get("completed_at")) is not int
        or not 0 <= row["completed_at"] - row["started_at"] <= 600
    ):
        raise ValueError("Original session completion window unavailable")
    return {
        "cell": cell,
        "observable_parity": "pass",
        "full_ac04": "pending",
        "reason": "provider-request-refresh-and-full-denial-observations-unavailable",
        "evidence_sha256": digest(row),
    }


def collect_legacy(config_path, *, client_factory, **binding):
    path = Path(config_path)
    manifest = validate_manifest(flow._decode(private_file(path, 65536)), **binding)
    rows = []
    for cell in CELLS:
        if manifest["cells"][cell] is None:
            rows.append(
                {
                    "cell": cell,
                    "status": "pending",
                    "reason": "missing-provider-consent-or-fixture",
                }
            )
            continue
        driver = LegacyCell(
            path.parent / cell.replace(":", "-"),
            cell,
            manifest,
            client_factory=client_factory,
        )
        try:
            rows.append(driver.run())
        except (
            ValueError,
            KeyError,
            TypeError,
            OSError,
            TimeoutError,
            httpx.HTTPError,
        ):
            rows.append(
                {
                    "cell": cell,
                    "status": "pending",
                    "reason": "original-legacy-campaign-unavailable",
                }
            )
        finally:
            try:
                driver.close()
            except (
                ValueError,
                KeyError,
                TypeError,
                OSError,
                TimeoutError,
                httpx.HTTPError,
            ):
                rows[-1].update(
                    status="pending", reason="original-proxy-cleanup-unconfirmed"
                )
    return {
        "schema": "legacy-live-observations/v1",
        "binding": manifest["binding"],
        "target": manifest["target"],
        "cases": rows,
        "full_ac04": "pending",
    }


def qualify_legacy(report, *, manifest):
    if (
        type(report) is not dict
        or report.get("schema") != "legacy-live-observations/v1"
        or report.get("binding") != manifest["binding"]
        or report.get("target") != manifest["target"]
        or type(report.get("cases")) is not list
        or len(report["cases"]) != len(CELLS)
        or report.get("full_ac04") != "pending"
    ):
        raise ValueError("Complete original legacy observation inventory required")
    seen, rows = set(), []
    for row in report["cases"]:
        cell = row.get("cell")
        if cell not in CELLS or cell in seen:
            raise ValueError("Duplicate or missing legacy cell")
        seen.add(cell)
        if row.get("status") == "pass":
            rows.append(qualify_cell(row, manifest=manifest))
        elif row.get("status") == "pending" and row.get("reason"):
            rows.append(
                {
                    "cell": cell,
                    "observable_parity": "pending",
                    "full_ac04": "pending",
                    "reason": row["reason"],
                }
            )
        else:
            raise ValueError("Unqualified legacy outcome")
    return {
        "binding": manifest["binding"],
        "target": manifest["target"],
        "cases": rows,
        "full_ac04": "pending",
    }
