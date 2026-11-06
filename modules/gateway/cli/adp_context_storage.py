#!/usr/bin/env python3
"""Product Context storage commands. Signed transfer links never go to stdout."""

import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import adp_common as common

MAX_BYTES = 50 * 1024 * 1024


def add_parser(commands):
    actions = commands.add_parser("storage").add_subparsers(dest="storage_action", required=True)
    for action in ("show", "configure", "upload", "complete", "download"):
        p = actions.add_parser(action)
        p.add_argument("--product", required=True)
        p.add_argument("--json", action="store_true")
        if action in {"configure", "upload", "complete"}:
            p.add_argument("--yes", action="store_true")
            p.add_argument("--dry-run", action="store_true")
        if action == "configure":
            p.add_argument("--encryption", choices=["shared", "managed", "customer"], required=True)
            p.add_argument("--revision", type=int, required=True, help="Current revision from storage show")
            p.add_argument("--kms-key-arn")
        if action == "upload":
            p.add_argument("--file", required=True)
        if action == "complete":
            p.add_argument("--upload-id", required=True, help="Retry completion of an already transferred document")
        if action == "download":
            p.add_argument("--asset-id", required=True)
            p.add_argument("--output", required=True, help="New destination file; existing files are never overwritten")


def scoped(value, product):
    if not isinstance(value, dict) or value.get("product_id") != product:
        raise common.CliError("Response does not match the requested Product.", "invalid_response", 5)
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transfer(url, *, payload=None, headers=None):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname:
        raise common.CliError("Storage returned an invalid transfer link.", "invalid_response", 5)
    # This client has no ADP bearer headers/cookies and will not follow redirects.
    request = urllib.request.Request(url, data=payload, headers=headers or {}, method="PUT" if payload is not None else "GET")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            result = response.read(MAX_BYTES + 1)
        if len(result) > MAX_BYTES:
            raise common.CliError("Downloaded document exceeds the size limit.", "invalid_response", 5)
        return result
    except (urllib.error.URLError, OSError):
        raise common.CliError("Document transfer failed. Check storage access and retry.", "storage_transfer_failed", 5) from None


def finish(client, path, product, upload_id):
    try:
        value = scoped(client.request("POST", path + "/uploads/" + upload_id + "/complete"), product)
    except common.CliError:
        raise common.CliError(
            "Document transferred; completion is unconfirmed. Retry: "
            f"adp knowledge storage complete --product {product} --upload-id {upload_id} --yes",
            "storage_completion_pending",
            4,
        ) from None
    if not isinstance(value.get("id"), str) or value.get("status") not in {"registered", "queued", "indexing", "indexed", "complete", "failed"}:
        raise common.CliError("Unconfirmed import acknowledgement; retry completion with the same upload ID.", "storage_completion_pending", 4)
    return common.envelope(
        "pending",
        "knowledge storage complete",
        {"product_id": product, "upload_id": upload_id, "asset_id": value["id"], "status": value["status"]},
        "Use knowledge status to check indexing readiness.",
    )


def execute(args, client):
    product, action = args.product, args.storage_action
    path = "/agent-context/products/" + urllib.parse.quote(product, safe="") + "/storage"
    command = "knowledge storage " + action
    if action == "show":
        value = scoped(client.request("GET", path), product)
        fields = {
            "product_id",
            "bucket",
            "prefix",
            "location",
            "revision",
            "encryption",
            "kms_key_arn",
            "can_configure",
            "can_upload",
            "max_upload_bytes",
            "extensions",
        }
        return common.envelope("ok", command, {k: v for k, v in value.items() if k in fields})
    if action == "download":
        target = Path(args.output)
        if target.exists():
            raise common.CliError("Choose a new output file.", "usage_error", 1)
        value = scoped(client.request("POST", path + "/assets/" + args.asset_id + "/download"), product)
        if value.get("asset_id") != args.asset_id:
            raise common.CliError("Response does not match the document.", "invalid_response", 5)
        data = transfer(value["url"])
        # Exclusive creation closes the exists/open race and protects local data.
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            output.write(data)
        return common.envelope("ok", command, {"product_id": product, "asset_id": args.asset_id, "bytes": len(data)})
    body, payload = {}, None
    if action == "configure":
        if args.revision < 0 or (args.encryption == "customer") != bool(args.kms_key_arn):
            raise common.CliError("Use a nonnegative revision and a key ARN only with customer encryption.", "usage_error", 1)
        body = {"revision": args.revision, "encryption": args.encryption}
        if args.kms_key_arn:
            body["kms_key_arn"] = args.kms_key_arn
    elif action == "upload":
        with Path(args.file).open("rb") as source:
            payload = source.read(MAX_BYTES + 1)
        if not 0 < len(payload) <= MAX_BYTES:
            raise common.CliError("Choose a nonempty document up to 50 MiB.", "usage_error", 1)
        body = {"filename": Path(args.file).name, "content_length": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    if args.dry_run or not args.yes:
        return common.envelope("preview", command, {"product_id": product, **body, "confirm": "--yes"})
    common.ensure_can_mutate("knowledge.asset.write")
    if action == "configure":
        value = scoped(client.request("PUT", path, body), product)
        return common.envelope("ok", command, {k: value[k] for k in ("product_id", "revision", "encryption", "kms_key_arn")})
    if action == "complete":
        from uuid import UUID

        return finish(client, path, product, str(UUID(args.upload_id)))
    receipt = scoped(client.request("POST", path + "/uploads", body), product)
    from uuid import UUID

    upload_id = str(UUID(receipt["upload_id"]))
    transfer(receipt["url"], payload=payload, headers=receipt["headers"])
    return finish(client, path, product, upload_id)
