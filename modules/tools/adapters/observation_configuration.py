"""Prepare adapter configuration from an existing Identity grant and live MCP schemas.

This operator command creates a candidate configuration, never an acceptance
report. It neither creates consent nor changes a running deployment.
"""

import argparse
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from adp_tools.adapter_contracts import AdapterRelease, Reference, WireModel
from pydantic import Field, model_validator

from tool_adapters.atlassian import (
    MAX_CATALOGUE_BYTES,
    MAX_CATALOGUE_PAGES,
    MAX_CATALOGUE_TOOLS,
    MAX_DEFINITION_BYTES,
    READ_TOOLS,
    WRITE_TOOLS,
    ReadMCPTransport,
)
from tool_adapters.atlassian_observe import (
    PROTOCOL_VERSION,
    SITES_TOOL,
    SUBJECT_TOOL,
    canonical,
    collect_observation,
)
from tool_adapters.config import AdapterConfiguration, ObservationBindings, ObservationConfiguration, ObservationProfile

RESOURCE = "https://mcp.atlassian.com/v2/mcp"
READ_SCOPES = tuple(scope for _, scope in READ_TOOLS.values())
IDENTITY_SCOPES = ("read:me", "read:account", "offline_access")


class Preparation(WireModel):
    release: AdapterRelease
    bindings: ObservationBindings
    observer_role_arn: str = Field(pattern=r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")
    owner_id: Reference
    expected_provider_user_id: Reference
    site_id: Reference
    enable_system_context: bool = Field(default=False, strict=True)
    enable_reads: bool = Field(default=False, strict=True)
    enable_writes: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def consistent_authority(self):
        account = self.observer_role_arn.split(":")[4]
        references = (
            self.bindings.core_role_arn,
            self.bindings.assertion_key_arn,
            self.bindings.gateway_role_arn,
            self.bindings.reconciler_role_arn,
        )
        prefix = self.bindings.identity_workload.removesuffix("-atlassian-mcp-oauth-user")
        if (
            any(reference and reference.split(":")[4] != account for reference in references)
            or self.bindings.identity_provider != prefix + "-atlassian-mcp-user"
            or (
                self.bindings.legacy_identity_workload is not None
                and self.bindings.legacy_identity_workload != prefix + "-oauth-user"
            )
            or (self.enable_reads and self.bindings.gateway_role_arn is None)
            or ((self.enable_writes or self.enable_system_context) and not self.enable_reads)
        ):
            raise ValueError("adapter_observation_authority_mismatch")
        return self


def _token(value, field):
    token = value.get(field) if isinstance(value, dict) else None
    if not isinstance(token, str) or not token or len(token) > 16384 or any(char.isspace() for char in token):
        raise ValueError("identity_grant_unavailable")
    return token


def _catalogue(token, *, transport_factory):
    deadline = time.monotonic() + 20
    transport = transport_factory(deadline=deadline)
    initial = transport.send(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "adp-adapter-configuration", "version": "1"},
        },
        token,
    )
    if (
        not isinstance(initial, dict)
        or "error" in initial
        or not isinstance(initial.get("result"), dict)
        or initial["result"].get("protocolVersion") != PROTOCOL_VERSION
    ):
        raise ValueError("provider_protocol_mismatch")
    transport.send("notifications/initialized", {}, token)
    tools, seen, cursor, size = {}, set(), None, 0
    for _ in range(MAX_CATALOGUE_PAGES):
        reply = transport.send("tools/list", {"cursor": cursor} if cursor else {}, token)
        page = reply.get("result") if isinstance(reply, dict) and "error" not in reply else None
        if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
            raise ValueError("catalogue_incomplete")
        for tool in page["tools"]:
            if (
                not isinstance(tool, dict)
                or not isinstance(tool.get("name"), str)
                or not tool["name"]
                or tool["name"] in tools
            ):
                raise ValueError("catalogue_incomplete")
            definition_size = len(canonical(tool))
            size += definition_size
            if (
                definition_size > MAX_DEFINITION_BYTES
                or size > MAX_CATALOGUE_BYTES
                or len(tools) >= MAX_CATALOGUE_TOOLS
            ):
                raise ValueError("catalogue_incomplete")
            tools[tool["name"]] = tool
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512 or cursor in seen:
            raise ValueError("catalogue_incomplete")
        seen.add(cursor)
    else:
        raise ValueError("catalogue_incomplete")
    if time.monotonic() >= deadline:
        raise ValueError("provider_deadline_exceeded")
    return tools


def _schema(tools, name):
    schema = tools.get(name, {}).get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise ValueError("provider_schema_unavailable")
    return schema, hashlib.sha256(canonical(schema)).hexdigest()


def prepare(request, *, sts_client, identity_client, transport_factory=ReadMCPTransport):
    """Use the selected role; token and provider profile data never enter the output."""
    request = Preparation.model_validate(request)
    role = request.observer_role_arn
    account = role.split(":")[4]
    caller = sts_client.get_caller_identity()
    if (
        not isinstance(caller, dict)
        or caller.get("Account") != account
        or not isinstance(caller.get("Arn"), str)
        or not caller["Arn"].startswith(f"arn:aws:sts::{account}:assumed-role/{role.rsplit('/', 1)[-1]}/")
    ):
        raise ValueError("selected_observer_role_mismatch")
    scopes = READ_SCOPES + IDENTITY_SCOPES + (("write:jira:agent-interface",) if request.enable_writes else ())
    workload = identity_client.get_workload_access_token_for_user_id(
        workloadName=request.bindings.identity_workload,
        userId=request.owner_id,
    )
    result = identity_client.get_resource_oauth2_token(
        workloadIdentityToken=_token(workload, "workloadAccessToken"),
        resourceCredentialProviderName=request.bindings.identity_provider,
        scopes=list(scopes),
        resources=[RESOURCE],
        oauth2Flow="USER_FEDERATION",
    )
    if not isinstance(result, dict) or result.get("sessionStatus") in {"IN_PROGRESS", "FAILED"}:
        raise ValueError("identity_grant_unavailable")
    token = _token(result, "accessToken")
    tools = _catalogue(token, transport_factory=transport_factory)
    settings = {
        **request.bindings.model_dump(),
        "subject_schema_sha256": _schema(tools, SUBJECT_TOOL)[1],
        "sites_schema_sha256": _schema(tools, SITES_TOOL)[1],
        "write_enabled": request.enable_writes,
        "system_context_enabled": request.enable_system_context,
    }
    for enabled, name, operations in (
        (request.enable_reads, "read", READ_TOOLS),
        (request.enable_writes, "write", WRITE_TOOLS),
    ):
        if enabled:
            pairs = {alias: _schema(tools, upstream) for alias, (upstream, _) in operations.items()}
            settings[name + "_schemas"] = {alias: pair[0] for alias, pair in pairs.items()}
            settings[name + "_schemas_sha256"] = {alias: pair[1] for alias, pair in pairs.items()}
    observation = ObservationConfiguration.model_validate(settings)
    subject, sites, _ = collect_observation(token, observation, transport_factory=transport_factory)
    selected = next((site for site in sites if site.site_id == request.site_id), None)
    if (
        subject != request.expected_provider_user_id
        or selected is None
        or not set(READ_SCOPES) & set(selected.scopes)
        or request.enable_writes
        and "write:jira:agent-interface" not in selected.scopes
    ):
        raise ValueError("provider_owner_or_site_mismatch")
    return AdapterConfiguration(
        schema_version="1.0",
        mode="observation",
        release=request.release,
        observation=observation,
    )


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_preparation_field")
        result[name] = value
    return result


def read_request(path):
    path = Path(path)
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077 or path.parent.stat().st_uid != os.getuid():
        raise ValueError("private_preparation_required")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        details = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_mode & 0o077
            or details.st_uid != os.getuid()
            or details.st_size > 16384
        ):
            raise ValueError("private_preparation_required")
        content = stream.read(16385)
    if len(content) > 16384:
        raise ValueError("private_preparation_required")
    return Preparation.model_validate(json.loads(content, object_pairs_hook=_unique_object))


def main():
    import boto3
    from botocore.config import Config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--profile", action="store_true", help="Bind prepared settings to source; publication supplies the image digest"
    )
    args = parser.parse_args()
    try:
        request = read_request(args.request)
        output = Path(args.output)
        if output.exists() or output.is_symlink() or output.parent.is_symlink() or output.parent.stat().st_mode & 0o077:
            raise ValueError("private_new_output_required")
        session = boto3.Session(region_name=request.bindings.assertion_key_arn.split(":")[3])
        sdk = Config(
            connect_timeout=3,
            read_timeout=10,
            retries={"total_max_attempts": 1},
            ignore_configured_endpoint_urls=True,
        )
        configured = prepare(
            request,
            sts_client=session.client("sts", config=sdk),
            identity_client=session.client("bedrock-agentcore", config=sdk),
        )
        if args.profile:
            configured = ObservationProfile(
                schema_version="1.0",
                mode="observation",
                source_revision=request.release.source_revision,
                observation=configured.observation,
            )
        descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(configured.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        raise SystemExit("adapter_observation_configuration_unavailable") from None
    print("Adapter observation configuration prepared; live acceptance remains required")


if __name__ == "__main__":
    main()
