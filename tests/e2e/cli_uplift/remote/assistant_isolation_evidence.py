"""Verify trusted qualification receipts before interpreting the isolation matrix."""

import hashlib
import json
import re

ACTORS = ("a1", "a2", "b1")
OWNER = (
    "session_read",
    "session_write",
    "message_read",
    "message_write",
    "summary_read",
    "summary_write",
    "artifact_read",
    "artifact_write",
    "memory_read",
    "memory_write",
    "cursor_read",
    "object_read",
    "direct_url_read",
)
CROSS = OWNER + (
    "forged_principal",
    "forged_run",
    "forged_session",
    "forged_generation",
    "forged_user_header",
    "forged_tenant_header",
)
CREDENTIALS = (
    "environment",
    "proc",
    "projected_files",
    "aws_provider",
    "aws_cli",
    "sts_identity",
    "s3_read",
    "dynamodb_read",
    "secrets_read",
    "metadata",
    "kubernetes_api",
    "network_routes",
)
LIMITS = {
    "duration_seconds": 120,
    "probe_timeout_seconds": 5,
    "result_bytes": 65536,
    "model_turns": 0,
}
MANIFEST = {
    "version": "e45-v1",
    "probes": {
        actor: list(
            (OWNER if actor == "a1" else CROSS + ("residue_file", "residue_process"))
            + ("sandbox_identity",)
            + CREDENTIALS
        )
        for actor in ACTORS
    },
    "limits": LIMITS,
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


MANIFEST_DIGEST = digest(MANIFEST)


def wire(evidence):
    document = json.loads(json.dumps(evidence))
    for binding in list(document.get("requests", {}).values()) + [
        binding
        for result in document.get("results", {}).values()
        for binding in (
            result.get("binding", {}),
            (result.get("receipt") or {}).get("binding", {}),
        )
    ]:
        if "credential_epoch" in binding:
            binding["epoch"] = binding.pop("credential_epoch")
    return document


def require(condition, message):
    if not condition:
        raise ValueError(message)


def expected_outcome(name, actor):
    return (
        "distinct"
        if name == "sandbox_identity"
        else "absent"
        if name
        in {"environment", "proc", "projected_files", "residue_file", "residue_process"}
        else "allowed"
        if actor == "a1" and name in OWNER
        else "denied"
    )


def check_execution(actor, receipt):
    probes = receipt.get("observations")
    require(
        receipt.get("source") == "trusted_runtime",
        f"Untrusted qualification receipt/{actor}",
    )
    require(isinstance(probes, list), f"Missing isolation probes/{actor}")
    missing = set(MANIFEST["probes"][actor]) - {
        probe.get("probe") for probe in probes if isinstance(probe, dict)
    }
    require(
        not missing,
        f"Missing isolation probes: {', '.join(name + '/' + actor for name in sorted(missing))}",
    )
    seen = set()
    for probe in probes:
        require(isinstance(probe, dict), f"Malformed isolation probe/{actor}")
        name = probe.get("probe")
        label = f"{name}/{actor}"
        require(
            name in MANIFEST["probes"][actor] and name not in seen,
            f"Unexpected or repeated isolation probe {label}",
        )
        seen.add(name)
        expected = expected_outcome(name, actor)
        require(probe.get("executed") is True, f"Unexecuted isolation probe {label}")
        require(
            probe.get("exposure_digests") == [], f"Exposure in isolation probe {label}"
        )
        require(
            probe.get("outcome") == expected,
            f"Isolation probe {label} expected {expected}, observed {probe.get('outcome')}",
        )


def verify(evidence, users, evaluation_id, *, now):
    require(isinstance(evidence, dict), "Missing qualification evidence")
    evidence = json.loads(json.dumps(evidence))
    for binding in list(evidence.get("requests", {}).values()) + [
        binding
        for result in evidence.get("results", {}).values()
        for binding in (
            result.get("binding", {}),
            (result.get("receipt") or {}).get("binding", {}),
        )
    ]:
        if "epoch" in binding:
            require(
                "credential_epoch" not in binding and type(binding["epoch"]) is int,
                "Invalid evidence epoch",
            )
            binding["credential_epoch"] = binding.pop("epoch")
    requests, results = evidence.get("requests"), evidence.get("results")
    require(
        isinstance(requests, dict) and set(requests) == set(ACTORS),
        "Missing qualification admission",
    )
    require(
        isinstance(results, dict) and set(results) == set(ACTORS),
        "Missing qualification results",
    )
    started, finished = evidence.get("started_at"), evidence.get("finished_at")
    require(
        type(started) is int
        and type(finished) is int
        and started <= finished <= now
        and now - started <= 900,
        "Stale or future qualification evidence",
    )
    identities, runs, sessions, nonces, images, fixture_digests = (
        set(),
        set(),
        set(),
        set(),
        set(),
        set(),
    )
    rows = []
    for actor in ACTORS:
        binding, observed, fixture = requests[actor], results[actor], users[actor]
        require(
            isinstance(binding, dict) and isinstance(observed, dict),
            f"Missing qualification binding/{actor}",
        )
        require(
            binding.get("actor") == actor
            and binding.get("evaluation_id") == evaluation_id
            and binding.get("target") == evidence.get("target")
            and bool(binding.get("target"))
            and binding.get("user_id") == fixture["canonical_user_id"]
            and binding.get("tenant_id") == fixture["tenant_id"]
            and binding.get("manifest_digest") == MANIFEST_DIGEST
            and binding.get("manifest_version") == "e45-v1"
            and binding.get("limits") == LIMITS,
            f"Mismatched qualification binding/{actor}",
        )
        issued, expires = binding.get("issued_at"), binding.get("expires_at")
        require(
            type(issued) is int
            and type(expires) is int
            and started <= issued < expires <= issued + 120,
            f"Invalid qualification lifetime/{actor}",
        )
        for field in ("sandbox_uid", "run_id", "session_id"):
            require(
                isinstance(binding.get(field), str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", binding[field]),
                f"Missing {field}/{actor}",
            )
        require(
            isinstance(binding.get("nonce"), str)
            and re.fullmatch(r"[a-f0-9]{64}", binding["nonce"]),
            f"Invalid nonce/{actor}",
        )
        require(
            type(binding.get("lease_generation")) is int
            and binding["lease_generation"] > 0,
            f"Invalid generation/{actor}",
        )
        require(
            type(binding.get("credential_epoch")) is int
            and binding["credential_epoch"] > 0
            and type(binding.get("attempt")) is int
            and binding["attempt"] > 0,
            f"Invalid execution epoch/{actor}",
        )
        require(
            isinstance(binding.get("image_digest"), str)
            and re.fullmatch(r"sha256:[a-f0-9]{64}", binding["image_digest"]),
            f"Missing image/{actor}",
        )
        receipt = observed.get("receipt")
        require(
            observed.get("state") == "finished"
            and observed.get("binding") == binding
            and isinstance(receipt, dict)
            and receipt.get("binding") == binding
            and receipt.get("source") == "trusted_runtime",
            f"Untrusted qualification receipt/{actor}",
        )
        require(
            receipt.get("status") == "completed" and receipt.get("failure") is None,
            f"Incomplete qualification execution/{actor}: {receipt.get('failure')}",
        )
        require(
            type(receipt.get("recorded_at")) is int
            and issued <= receipt["recorded_at"] < expires
            and receipt["recorded_at"] <= finished,
            f"Expired qualification result/{actor}",
        )
        require(
            receipt.get("result_digest")
            == digest(
                {key: value for key, value in receipt.items() if key != "result_digest"}
            ),
            f"Mismatched qualification receipt digest/{actor}",
        )
        require(
            isinstance(receipt.get("fixture_digest"), str)
            and re.fullmatch(r"[a-f0-9]{64}", receipt["fixture_digest"]),
            f"Missing bounded fixture evidence/{actor}",
        )
        require(
            observed.get("sandbox_removed") is True,
            f"Unobserved sandbox teardown/{actor}",
        )
        cleanup = evidence.get("cleanup", {}).get(actor, {})
        require(
            cleanup.get("cleanup_complete") is True
            and cleanup.get("nonce") == binding["nonce"],
            f"Unverified qualification cleanup/{actor}",
        )
        probes = receipt.get("observations")
        check_execution(actor, receipt)
        for probe in probes:
            name = probe.get("probe")
            expected = expected_outcome(name, actor)
            call_id = binding["nonce"] + ":" + name
            rows.append(
                {
                    "probe": name,
                    "actor": actor,
                    "target_owner": actor
                    if name in CREDENTIALS or name == "sandbox_identity"
                    else "a1",
                    "expected": expected,
                    "observed": {
                        "source": "sandbox_tool",
                        "executed": True,
                        "outcome": probe["outcome"],
                        "tool_call_id": call_id,
                        "sandbox_uid": binding["sandbox_uid"],
                    },
                    "evidence_ref": "qualification/" + call_id,
                }
            )
        identities.add(binding["sandbox_uid"])
        runs.add(binding["run_id"])
        sessions.add(binding["session_id"])
        nonces.add(binding["nonce"])
        images.add(binding["image_digest"])
        fixture_digests.add(receipt["fixture_digest"])
    require(
        all(len(values) == 3 for values in (identities, runs, sessions, nonces)),
        "Replayed or reused qualification identity",
    )
    require(
        len(images) == len(fixture_digests) == 1,
        "Qualification image or fixture changed between actors",
    )
    require(
        users["a1"]["tenant_id"] == users["a2"]["tenant_id"] != users["b1"]["tenant_id"]
        and len({users[actor]["canonical_user_id"] for actor in ACTORS}) == 3,
        "Invalid isolation actor matrix",
    )
    require(
        requests["a2"]["issued_at"] >= results["a1"]["receipt"]["recorded_at"]
        and requests["b1"]["issued_at"] >= results["a1"]["receipt"]["recorded_at"],
        "Residue probes preceded owner execution",
    )
    return {
        "rows": rows,
        "image_digest": next(iter(images)),
        "trusted_receipts_verified": True,
    }
