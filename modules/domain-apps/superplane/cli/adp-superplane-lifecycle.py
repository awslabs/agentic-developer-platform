#!/usr/bin/env python3
"""App-owned lifecycle extension loaded by the shared ADP Superplane dispatcher."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import time
import uuid
from urllib.parse import urlencode

import adp_common as common

BASE = "/superplane/v1"


def identifier(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise common.CliError(
            "Use the exact UUID from workspace or provider-connection readback.",
            "usage_error",
            1,
        ) from None


def revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise common.CliError(
            "Use the reviewed 64-character revision.", "usage_error", 1
        )
    return value


def configure(commands, workspaces, deployments, leaf, mutation, events):
    clusters = commands.add_parser(
        "cluster", help="Discover eligible cluster placement without provisioning"
    )
    cluster_subs = clusters.add_subparsers(dest="subcommand", required=True)
    cluster_list = leaf(cluster_subs, "list")
    cluster_list.add_argument(
        "--eligible-for", choices=["workspace-sharing"], default="workspace-sharing"
    )
    profiles = leaf(
        deployments,
        "profiles",
        help="Read server-supported serving profiles and readiness",
    )
    profiles.add_argument("--workspace", required=True)
    delete = mutation(
        leaf(
            workspaces,
            "delete",
            help="Review and request teardown; billing cleanup remains separately verified",
        )
    )
    delete.add_argument("workspace")
    delete.add_argument("--expected-revision")
    delete.add_argument("--operation-id")
    provider = commands.add_parser(
        "provider-connection", help="Manage workspace-bound ADP credential references"
    )
    subs = provider.add_subparsers(dest="subcommand", required=True)
    for action in ("create", "show", "validate", "rotate", "revoke"):
        parser = leaf(subs, action)
        parser.add_argument("--workspace", required=True)
        if action != "create":
            parser.add_argument("--connection", required=True)
        if action != "show":
            mutation(parser)
            parser.add_argument("--operation-id")
            if action != "create":
                parser.add_argument("--expected-revision")
        if action in {"create", "rotate"}:
            parser.add_argument(
                "--credential-id",
                required=True,
                help="Opaque existing ADP credential reference, never a value or ARN",
            )
            parser.add_argument("--service", required=True)
            parser.add_argument("--label", required=True)
        if action == "create":
            parser.add_argument("--provider", required=True)
        if action in {"validate", "rotate"}:
            parser.add_argument(
                "--validation-file",
                required=True,
                help="Private file of provider readings; the server must independently attest it",
            )
    events.add_argument(
        "--workspace",
        help="Exact workspace UUID; uses bounded workspace-attributed audit records",
    )
    events.add_argument("--follow", action="store_true")
    events.add_argument(
        "--after", help="Resume cursor from a previous workspace event response"
    )
    events.add_argument(
        "--timeout", type=int, default=60, choices=range(1, 301), metavar="1..300"
    )
    events.add_argument(
        "--max-pages", type=int, default=10, choices=range(1, 101), metavar="1..100"
    )


def workspace_snapshot(value, workspace):
    if (
        not isinstance(value, dict)
        or value.get("workspace_id") != workspace
        or not isinstance(value.get("status"), str)
        or type(value.get("is_default")) is not bool
        or not all(
            isinstance(value.get(key), list)
            for key in ("deployments", "provider_connections", "provider_handles")
        )
        or value.get("billing_state") != "unconfirmed"
    ):
        raise common.CliError(
            "Malformed workspace lifecycle snapshot.", "invalid_response", 5
        )
    revision(value.get("revision"))
    return value


def connection_snapshot(value, workspace, connection):
    if (
        not isinstance(value, dict)
        or value.get("workspace_id") != workspace
        or value.get("connection_id") != connection
        or value.get("status") not in {"pending", "active", "disabled"}
        or not isinstance(value.get("credential"), dict)
        or not isinstance(value.get("binding"), dict)
        or value["binding"].get("workspace_id") != workspace
        or type(value.get("admits_new_work")) is not bool
        or type(value.get("allows_renewal")) is not bool
    ):
        raise common.CliError(
            "Malformed provider connection or workspace binding.", "invalid_response", 5
        )
    revision(value.get("revision"))
    return value


def reference(args):
    value = {
        "credential_id": args.credential_id,
        "service": args.service,
        "label": args.label,
    }
    if any(
        not isinstance(text, str) or not text or len(text) > 255 or "\n" in text
        for text in value.values()
    ):
        raise common.CliError(
            "Credential reference metadata is missing or too long.", "usage_error", 1
        )
    key = value["credential_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", key) or key.startswith(
        ("AKIA", "ASIA", "sk-")
    ):
        raise common.CliError(
            "Supply an opaque credential ID, never secret material or an ARN.",
            "usage_error",
            1,
        )
    return value


def validation(path):
    body = common.read_private_json(path)
    keys = {
        "credential_valid",
        "permissions_sufficient",
        "quota_available",
        "observed_capacity",
    }
    if (
        not isinstance(body, dict)
        or set(body) - keys
        or any(type(body.get(key)) is not bool for key in keys - {"observed_capacity"})
    ):
        raise common.CliError(
            "Validation file requires three boolean readings and optional observed_capacity.",
            "usage_error",
            1,
        )
    count = body.get("observed_capacity")
    if count is not None and (type(count) is not int or not 0 <= count <= 2147483647):
        raise common.CliError(
            "observed_capacity must be null or an integer 0..2147483647.",
            "usage_error",
            1,
        )
    return body


def mutate_once(
    args, api, command, path, method, body, before, readback, *, reconcile=None
):
    if args.dry_run or not args.yes:
        return common.envelope(
            "dry_run",
            command,
            {
                "before": before,
                "request": body,
                "expected_revision": before.get("revision") if before else None,
            },
            "Review the exact state, then supply --yes and a stable --operation-id. No mutation was sent.",
        )
    operation = identifier(args.operation_id)
    binding = {
        "gateway": common.gateway_url(),
        "scope": common.authenticated_scope(),
        "path": path,
        "method": method,
        "body": body,
    }
    fingerprint = hashlib.sha256(
        json.dumps(binding, sort_keys=True).encode()
    ).hexdigest()
    directory = common.private_directory(common.state_dir() / "superplane-lifecycle")
    receipt_path = directory / (operation + ".json")
    with common.file_lock(
        receipt_path.with_suffix(".lock"), "Lifecycle operation already running"
    ):
        if receipt_path.exists():
            receipt = common.read_private_json(receipt_path)
            if receipt.get("fingerprint") != fingerprint:
                raise common.CliError(
                    "Operation ID belongs to different lifecycle inputs.", "conflict", 4
                )
            observed = reconcile() if reconcile is not None else before
            return common.envelope(
                "pending",
                command,
                {
                    "operation_id": operation,
                    "observed": observed,
                    "acknowledgement": receipt.get("acknowledgement"),
                    "replayed_without_write": True,
                },
                "No mutation replayed; inspect the canonical domain operation and provider cleanup.",
            )
        if before is not None and args.expected_revision != before["revision"]:
            raise common.CliError(
                "The expected revision is missing or differs from current readback.",
                "conflict",
                4,
            )
        common.ensure_can_mutate("superplane.workspace.write", request=api.request)
        receipt = {"fingerprint": fingerprint}
        common.write_json(receipt_path, receipt)
        acknowledged = False
        try:
            result = api.request(method, path, body)
            acknowledged = True
            if not isinstance(result, dict):
                raise common.CliError(
                    "Missing lifecycle acknowledgement.", "invalid_response", 5
                )
            observed = readback(result)
            result = {
                key: result[key]
                for key in ("id", "connection_id", "status")
                if key in result
            }
            receipt["acknowledgement"] = result
            common.write_json(receipt_path, receipt)
        except common.CliError as exc:
            if not acknowledged and exc.status_code in {
                400,
                401,
                403,
                404,
                409,
                422,
            }:
                raise
            return common.envelope(
                "pending",
                command,
                {"operation_id": operation, "outcome": "unknown"},
                "No retry sent; inspect the same workspace or connection.",
            )
        except (OSError, ValueError, http.client.HTTPException, KeyboardInterrupt):
            return common.envelope(
                "pending",
                command,
                {"operation_id": operation, "outcome": "unknown"},
                "No retry sent; inspect the same workspace or connection.",
            )
        return common.envelope(
            "pending",
            command,
            {
                "operation_id": operation,
                "acknowledgement": result,
                "observed": observed,
            },
            "Domain acknowledgement is not proof of stopped billing, revoked provider credentials, or completed workloads.",
        )


def workspace_delete(args, api):
    workspace = identifier(args.workspace)
    base = BASE + "/workspaces/" + workspace
    before = workspace_snapshot(api.request("GET", base + "/lifecycle"), workspace)
    if before["is_default"]:
        return common.envelope(
            "unavailable",
            "superplane workspace delete",
            {"before": before, "reason": "protected_default_workspace"},
        )
    query = (
        "?" + urlencode({"expected_revision": revision(args.expected_revision)})
        if args.expected_revision
        else ""
    )

    def readback(answer):
        if answer.get("id") != workspace or not isinstance(answer.get("status"), str):
            raise common.CliError(
                "Teardown acknowledgement target mismatch.", "invalid_response", 5
            )
        return workspace_snapshot(api.request("GET", base + "/lifecycle"), workspace)

    return mutate_once(
        args,
        api,
        "superplane workspace delete",
        base + query,
        "DELETE",
        None,
        before,
        readback,
    )


def provider_connection(args, api):
    workspace = identifier(args.workspace)
    base = BASE + "/workspaces/" + workspace + "/provider-connections"
    before = None
    connection = None
    if args.subcommand != "create":
        connection = identifier(args.connection)
        base += "/" + connection
        before = connection_snapshot(api.request("GET", base), workspace, connection)
    if args.subcommand == "show":
        return common.envelope("ok", "superplane provider-connection show", before)
    action = args.subcommand
    if action == "create" and args.operation_id:
        connection = identifier(args.operation_id)
    if action == "create" and args.yes and not args.dry_run:
        connection = identifier(args.operation_id)
        capabilities = api.request("GET", BASE + "/capabilities")
        features = (
            capabilities.get("features") if isinstance(capabilities, dict) else None
        )
        if (
            not isinstance(features, list)
            or "provider-connection-operation-id-v1" not in features
        ):
            raise common.CliError(
                "Domain does not support recoverable provider registration; nothing was written.",
                "unavailable",
                4,
            )
    body = (
        {
            **reference(args),
            "provider": args.provider,
            **({"operation_id": connection} if connection else {}),
        }
        if action == "create"
        else validation(args.validation_file)
        if action == "validate"
        else {
            "replacement": reference(args),
            "validation": validation(args.validation_file),
        }
        if action == "rotate"
        else None
    )
    path = base + ({"validate": "/validation", "rotate": "/rotation"}.get(action, ""))
    if before is not None and args.expected_revision:
        path += "?" + urlencode({"expected_revision": revision(args.expected_revision)})

    def readback(answer):
        key = connection or identifier(answer.get("connection_id"))
        if answer.get("connection_id") != key:
            raise common.CliError(
                "Connection acknowledgement target mismatch.", "invalid_response", 5
            )
        observed = connection_snapshot(
            api.request(
                "GET",
                BASE + "/workspaces/" + workspace + "/provider-connections/" + key,
            ),
            workspace,
            key,
        )
        expected = (
            body
            if action == "create"
            else body["replacement"]
            if action == "rotate"
            else None
        )
        if expected and any(
            observed["credential"].get(field) != expected[field]
            for field in ("credential_id", "service", "label")
        ):
            raise common.CliError(
                "Connection credential readback mismatch.", "invalid_response", 5
            )
        if action == "create" and observed.get("provider") != args.provider:
            raise common.CliError(
                "Connection provider readback mismatch.", "invalid_response", 5
            )
        if action == "revoke" and (
            observed["status"] != "disabled"
            or observed["admits_new_work"]
            or observed["allows_renewal"]
            or not observed.get("limitation")
        ):
            raise common.CliError(
                "Connection disablement readback mismatch.", "invalid_response", 5
            )
        return observed

    return mutate_once(
        args,
        api,
        "superplane provider-connection " + action,
        path,
        "DELETE" if action == "revoke" else "POST",
        body,
        before,
        readback,
        reconcile=(lambda: readback({"connection_id": connection}))
        if action == "create"
        else None,
    )


def events(args, api):
    workspace = identifier(args.workspace)
    if (
        not 1 <= args.limit <= 100
        or args.offset is not None
        or any(
            getattr(args, key, None)
            for key in (
                "resource_type",
                "user",
                "action",
                "event_type",
                "start_time",
                "end_time",
            )
        )
    ):
        raise common.CliError(
            "Workspace events use --after, --limit 1..100, --max-pages and --timeout; organization filters cannot be combined.",
            "usage_error",
            1,
        )
    deadline = time.monotonic() + args.timeout
    cursor = args.after
    pages = 0
    while pages < args.max_pages and time.monotonic() < deadline:
        params = {"limit": args.limit}
        if cursor:
            params["after"] = cursor
        value = api.request(
            "GET", BASE + "/events/workspaces/" + workspace + "?" + urlencode(params)
        )
        if (
            not isinstance(value, dict)
            or value.get("workspace_id") != workspace
            or not isinstance(value.get("events"), list)
            or len(value["events"]) > args.limit
            or type(value.get("has_more")) is not bool
            or not (
                value.get("next_cursor") is None
                or isinstance(value["next_cursor"], str)
            )
        ):
            raise common.CliError(
                "Malformed workspace event page.", "invalid_response", 5
            )
        for event in value["events"]:
            if not isinstance(event, dict) or not event.get("id"):
                raise common.CliError(
                    "Malformed workspace event.", "invalid_response", 5
                )
        if value["events"] and value["next_cursor"] == cursor:
            raise common.CliError(
                "Workspace event cursor did not advance.", "invalid_response", 5
            )
        cursor = value["next_cursor"]
        pages += 1
        envelope = common.envelope("ok", "superplane events", value)
        if not args.follow:
            return envelope
        common.emit(envelope, True)
        if not value["has_more"]:
            time.sleep(min(2, max(0, deadline - time.monotonic())))
    return common.envelope(
        "pending",
        "superplane events",
        {"workspace_id": workspace, "next_cursor": cursor, "pages": pages},
        "Bounded follow ended; resume with --after. This is an attributed audit feed, not provider completion proof.",
    )


def cluster_list(args, api):
    value = api.request("GET", BASE + "/workspaces?view=eligible-clusters")
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("clusters"), list)
        or len(value["clusters"]) > 1000
    ):
        raise common.CliError(
            "Malformed eligible-cluster response.", "invalid_response", 5
        )
    for cluster in value["clusters"]:
        if (
            not isinstance(cluster, dict)
            or not cluster.get("id")
            or type(cluster.get("platform_eligible")) is not bool
        ):
            raise common.CliError("Malformed eligible cluster.", "invalid_response", 5)
    return common.envelope(
        "ok",
        "superplane cluster list",
        value,
        "Eligibility is server-authorized placement metadata, not cluster readiness or provisioning.",
    )


def deployment_profiles(args, api):
    workspace = identifier(args.workspace)
    value = api.request(
        "GET", BASE + "/workspaces/" + workspace + "/deployment-profiles"
    )
    if (
        not isinstance(value, dict)
        or value.get("workspace_id") != workspace
        or not isinstance(value.get("profiles"), list)
        or type(value.get("can_submit")) is not bool
    ):
        raise common.CliError(
            "Malformed deployment profile catalogue.", "invalid_response", 5
        )
    return common.envelope(
        "ok",
        "superplane deploy profiles",
        value,
        "Installed profiles govern supported values; this is not proof of GPU availability.",
    )
