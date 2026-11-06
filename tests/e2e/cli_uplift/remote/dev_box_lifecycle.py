"""E51: admitted owner operations on the existing assistant EC2 evaluation host."""

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import assistant_baseline
import assistant_isolation_clients
import common

IDENTIFIER = re.compile(r"[A-Za-z0-9-]{1,128}\Z")


def request(gateway, session, path, *, method="GET", body=None, key=None):
    common.require(
        (path == "/cloud-desktops" or path.startswith("/cloud-desktops/"))
        and ".." not in path,
        "Invalid Dev Box path",
    )
    url = gateway.rstrip("/") + path
    common.require(
        url.startswith("https://") and "@" not in gateway, "Dev Box requires HTTPS"
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    headers = {
        "Authorization": "Bearer " + session.token(),
        "Accept": "application/json",
    }
    if key is not None:
        common.require(IDENTIFIER.fullmatch(key) is not None, "Invalid operation key")
        headers["Idempotency-Key"] = key
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    wire = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect
        ).open(wire, timeout=15) as response:
            status, payload = response.status, response.read(65537)
    except urllib.error.HTTPError as error:
        status, payload = error.code, error.read(65537)
    common.require(len(payload) <= 65536, "Dev Box response exceeds limit")
    try:
        document = json.loads(payload)
    except ValueError:
        document = None
    return status, document


def verified_operation(gateway, session, receipt, *, timeout=300):
    box_id, operation_id = receipt.get("desktop_id"), receipt.get("operation_id")
    common.require(
        all(
            isinstance(value, str) and IDENTIFIER.fullmatch(value)
            for value in (box_id, operation_id)
        ),
        "Dev Box admission returned no scoped operation identity",
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, result = request(
            gateway, session, f"/cloud-desktops/{box_id}/operations/{operation_id}"
        )
        common.require(
            status == 200 and isinstance(result, dict),
            "Dev Box operation cannot be observed",
        )
        common.require(
            result.get("desktop_id") == box_id
            and result.get("operation_id") == operation_id,
            "Dev Box operation identity changed",
        )
        if result.get("status") == "succeeded":
            return result
        common.require(
            result.get("status") in {"pending", "unknown"}, "Dev Box operation failed"
        )
        time.sleep(2)
    raise common.RemoteError(
        "Dev Box operation remains pending; inspect recorded operation before retry"
    )


def verify_host_identity(gateway, sessions, fixtures, actor, box_id, probe):
    other = "b1" if actor == "a1" else "a1"
    digest = probe.public_key_digest(actor, box_id)
    common.require(
        isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
        "Host identity probe requires a public key digest",
    )
    path = f"/cloud-desktops/{box_id}/host-enrollment"
    body = {"public_key_digest": digest}
    status, approved = request(gateway, sessions[actor], path, method="POST", body=body)
    common.require(
        status == 201
        and isinstance(approved, dict)
        and isinstance(approved.get("device_id"), str)
        and IDENTIFIER.fullmatch(approved["device_id"])
        and isinstance(approved.get("challenge"), str)
        and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", approved["challenge"]),
        "Current owner cannot approve the verified host",
    )
    denied, foreign_body = request(
        gateway, sessions[other], path, method="POST", body=body
    )
    common.require(denied == 404, "Another owner can approve the host")
    replay, replay_body = request(
        gateway, sessions[actor], path, method="POST", body=body
    )
    common.require(replay == 409, "Host enrollment challenge can be replayed")
    for response in (foreign_body, replay_body):
        serialized = json.dumps(response)
        common.require(
            all(
                value not in serialized
                for value in (box_id, approved["device_id"], approved["challenge"])
            ),
            "Denied host enrollment disclosed machine scope",
        )
    run_id = probe.attest(actor, box_id, approved["device_id"], approved["challenge"])
    common.require(
        isinstance(run_id, str) and re.fullmatch(r"[0-9a-f-]{36}", run_id),
        "Host attestation did not create a scoped run",
    )
    status, attribution = probe.invoke(actor, box_id, run_id, "current")
    expected = fixtures[actor]
    common.require(
        status == 200
        and isinstance(attribution, dict)
        and attribution.get("root_human_id") == expected["canonical_user_id"]
        and attribution.get("tenant_id") == expected["tenant_id"],
        "Host model use lacks verified human ownership",
    )
    for invalid in (
        "foreign_instance",
        "foreign_role",
        "foreign_run",
        "direct_bedrock",
        "other_route",
    ):
        status, _ = probe.invoke(actor, box_id, run_id, invalid)
        common.require(status == 403, "Host escaped its approved run or model route")
    return run_id


def execute(config, evidence, *, identity_probe=None):
    settings = config.get("dev_box_lifecycle") or {}
    common.require(
        settings.get("approved") is True
        and all(
            isinstance(settings.get(name), str) and settings[name]
            for name in ("template", "profile")
        )
        and config.get("evaluation_id")
        and config.get("work_dir"),
        "E51 requires an approved, budgeted Dev Box fixture and evaluation directory",
    )
    gateway = config["gateway_url"].rstrip("/")
    sessions = assistant_isolation_clients.fixture_sessions(config)
    assistant_isolation_clients.verify_identities(config, sessions)
    journal = Path(config["work_dir"]) / (
        "dev-box-"
        + hashlib.sha256(str(config["evaluation_id"]).encode()).hexdigest()
        + ".json"
    )
    common.require(
        not journal.exists() and not config.get("prior_dispatch"),
        "E51 has prior intent; reconcile and release recorded boxes before rerunning",
    )
    record = {"evaluation_id": config["evaluation_id"], "operations": [], "boxes": {}}

    def persist():
        assistant_baseline.persist(journal, record)

    def act(actor, action, box_id=None):
        key = hashlib.sha256(
            f"{config['evaluation_id']}:{actor}:{action}:{len(record['operations'])}".encode()
        ).hexdigest()
        path = "/cloud-desktops" + (f"/{box_id}/{action}" if box_id else "")
        body = (
            {"template": settings["template"], "profile": settings["profile"]}
            if action == "setup"
            else {"workspace_retention": "delete"}
            if action == "release"
            else None
        )
        record["operations"].append(
            {
                "actor": actor,
                "action": action,
                "box_id": box_id,
                "key": key,
                "state": "intent",
            }
        )
        persist()
        status, receipt = request(
            gateway, sessions[actor], path, method="POST", body=body, key=key
        )
        common.require(
            status == 202 and isinstance(receipt, dict),
            "Dev Box operation was not admitted",
        )
        common.require(
            box_id is None or receipt.get("desktop_id") == box_id,
            "Dev Box operation changed box identity",
        )
        record["operations"][-1].update(
            box_id=receipt.get("desktop_id"), operation_id=receipt.get("operation_id")
        )
        persist()
        verified_operation(gateway, sessions[actor], receipt)
        record["operations"][-1]["state"] = "succeeded"
        persist()
        return receipt["desktop_id"]

    identity_checks = []

    def observe_host(actor, box_id, generation):
        marker = {
            "actor": actor,
            "box_id": box_id,
            "generation": generation,
            "state": "intent",
        }
        record.setdefault("host_identity", []).append(marker)
        persist()
        run_id = verify_host_identity(
            gateway, sessions, config["assistant_users"], actor, box_id, identity_probe
        )
        marker["state"] = "verified"
        persist()
        identity_checks.append(
            {"actor": actor, "generation": generation, "verified": True}
        )
        return run_id

    for actor in ("a1", "b1"):
        box_id = act(actor, "setup")
        record["boxes"][actor] = box_id
        persist()
        other = "b1" if actor == "a1" else "a1"
        for suffix in ("", "/resources"):
            status, _ = request(
                gateway, sessions[other], f"/cloud-desktops/{box_id}{suffix}"
            )
            common.require(
                status == 404, "Another owner can see the Dev Box or its inventory"
            )
        status, before = request(
            gateway, sessions[actor], f"/cloud-desktops/{box_id}/resources"
        )
        common.require(
            status == 200 and isinstance(before, dict),
            "Owned workspace inventory is unavailable",
        )
        disks = [
            item
            for item in before.get("items", [])
            if item.get("resource_kind") == "workspace_disk"
        ]
        common.require(
            len(disks) == 1 and disks[0].get("encrypted") is True,
            "Dev Box has no uniquely owned encrypted workspace",
        )
        volume_id = disks[0]["provider_resource_id"]
        run_id = None
        if identity_probe is not None:
            run_id = observe_host(actor, box_id, "initial")
        for action in ("stop", "start", "stop", "replace"):
            act(actor, action, box_id)
            if run_id is not None:
                status, _ = identity_probe.invoke(actor, box_id, run_id, "stale")
                common.require(
                    status == 403, "Stopped or replaced host run remains authorized"
                )
        if identity_probe is not None:
            run_id = observe_host(actor, box_id, "replacement")
        status, after = request(
            gateway, sessions[actor], f"/cloud-desktops/{box_id}/resources"
        )
        common.require(
            status == 200
            and len(
                [
                    item
                    for item in after.get("items", [])
                    if item.get("resource_kind") == "workspace_disk"
                    and item.get("provider_resource_id") == volume_id
                ]
            )
            == 1,
            "Replacement lost or changed the owned workspace",
        )
        act(actor, "release", box_id)
        if run_id is not None:
            status, _ = identity_probe.invoke(actor, box_id, run_id, "stale")
            common.require(status == 403, "Released host run remains authorized")
        status, released = request(
            gateway, sessions[actor], f"/cloud-desktops/{box_id}/resources"
        )
        common.require(
            status == 200
            and all(
                item.get("deletion_state") == "verified_deleted"
                for item in released.get("items", [])
            ),
            "Release left unverified owned inventory",
        )
    evidence.update(
        success=True,
        detail={
            "status": "BLOCKED",
            "operation_ids": [entry["operation_id"] for entry in record["operations"]],
            "two_owners": True,
            "identity_checks": identity_checks,
            "reason": (
                "Offline host identity and API observations do not establish deployed provider/SSM qualification"
                if identity_probe is not None
                else "Host identity unverified; API-only observations cannot establish provider/SSM qualification"
            ),
        },
    )
