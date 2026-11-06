"""Assertions shared by installed CLI and served browser guided journeys."""

import time

REQUIRED_CHECKS = {
    "idea": {"information", "project_scope"},
    "existing_assets": {"information", "project_scope", "repository"},
    "running_product": {
        "information",
        "project_scope",
        "repository",
        "environment",
        "pipeline",
        "operations",
    },
}
ALLOWED_PROPOSAL_FIELDS = {"name", "purpose", "intended_users", "domain", "goals"}
MODEL_PROPOSAL_FIELDS = {
    "id",
    "base_revision",
    "configuration",
    "product",
    "provenance",
    "remaining_paths",
    "expires_at",
}
MODEL_PROVENANCE_FIELDS = {"paths", "state", "method", "source_refs", "observed_at"}


def verify_guided_journey(
    starting_point, profile, session, browser_message, cli_guidance, independent_session
):
    if starting_point not in REQUIRED_CHECKS:
        raise ValueError("Unknown guided starting point")
    assert profile["product"]["starting_point"] == starting_point
    assert (
        profile["product"]["name"]
        and profile["product"]["purpose"]
        and profile["product"]["intended_users"]
    )
    assert session == independent_session
    assert session["progress"] == profile["progress"]
    checks = {check["id"]: check for check in session["progress"]["checks"]}
    assert REQUIRED_CHECKS[starting_point] <= checks.keys()
    assert (
        checks["information"]["status"]
        == checks["project_scope"]["status"]
        == "complete"
    )
    assert all(checks[name]["required"] for name in REQUIRED_CHECKS[starting_point])
    if starting_point == "idea":
        assert "repository" not in checks
        assert browser_message == "No missing steps in the current scope."
    else:
        assert checks["repository"]["status"] in ("missing", "complete")
        if checks["repository"]["status"] == "missing":
            assert browser_message.startswith(
                "Review a repository from your connected sources."
            )
        else:
            assert "repository" not in browser_message.lower()
        if starting_point == "running_product":
            assert all(
                checks[name]["status"] == "missing"
                for name in ("environment", "pipeline", "operations")
            )
            assert "Verify your environment connection." in browser_message
        elif checks["repository"]["status"] == "complete":
            assert browser_message == "No missing steps in the current scope."
    assert cli_guidance == {
        "revision": session["revision"],
        "message": browser_message,
        "proposals": [],
        "mode": "deterministic",
    }


def verify_direct_save(
    before_profile, before_session, cli_save, after_profile, after_session
):
    assert cli_save == after_session
    assert after_session["revision"] == before_session["revision"] + 1
    assert after_profile["revision"] == after_session["revision"]
    assert after_profile["product"]["purpose"] == "Confirmed by direct edit"
    assert before_profile["product"]["name"] == after_profile["product"]["name"]
    assert (
        before_profile["product"]["starting_point"]
        == after_profile["product"]["starting_point"]
    )
    assert before_profile["progress"]["checks"] == after_profile["progress"]["checks"]


def verify_pending_guidance_resume(
    first, resumed, independent, *, tenant_id, product_id, project_id
):
    if not isinstance(first, dict):
        raise AssertionError("Cross-client guidance resume needs a saved session")
    for observation in (first, resumed, independent):
        if (
            not isinstance(observation, dict)
            or any(
                observation.get(field) != expected
                for field, expected in (
                    ("tenant_id", tenant_id),
                    ("product_id", product_id),
                    ("project_id", project_id),
                )
            )
            or observation.get("id") != first.get("id")
            or observation.get("revision") != first.get("revision")
            or not isinstance(observation.get("proposals"), list)
        ):
            raise AssertionError(
                "Cross-client guidance resume changed scope, session or revision"
            )
    proposals = first["proposals"]
    if (
        not proposals
        or len(proposals) > 10
        or any(
            not isinstance(proposal, dict)
            or not isinstance(proposal.get("id"), str)
            or not proposal["id"]
            or not isinstance(proposal.get("remaining_paths"), list)
            or not proposal["remaining_paths"]
            or any(
                not isinstance(path, str) or not path.startswith("/")
                for path in proposal["remaining_paths"]
            )
            or len(set(proposal["remaining_paths"])) != len(proposal["remaining_paths"])
            for proposal in proposals
        )
        or len({proposal["id"] for proposal in proposals}) != len(proposals)
    ):
        raise AssertionError("Pending guidance must contain distinct, reviewable paths")
    if resumed["proposals"] != proposals or independent["proposals"] != proposals:
        raise AssertionError("Cross-client resume lost or changed pending proposals")
    return {
        "session_id": first["id"],
        "revision": first["revision"],
        "proposal_ids": [proposal["id"] for proposal in proposals],
        "remaining_paths": {
            proposal["id"]: proposal["remaining_paths"] for proposal in proposals
        },
    }


def verify_revoked_guidance_resume(
    revocation, pending, *, tenant_id, product_id, project_id
):
    if not isinstance(revocation, dict) or not isinstance(
        revocation.get("source_ref"), dict
    ):
        raise AssertionError(
            "Revocation needs the original authorized source reference"
        )
    source = revocation["source_ref"]
    if (
        set(source)
        != {
            "tenant_id",
            "project_id",
            "source_connection_id",
            "record_type",
            "record_id",
            "record_revision",
        }
        or source["tenant_id"] != tenant_id
        or source["project_id"] != project_id
        or any(not isinstance(value, str) or not value for value in source.values())
    ):
        raise AssertionError(
            "Revoked source must belong to the observed tenant and Project"
        )
    before = revocation.get("before_session")
    resumed = revocation.get("cli_session")
    independent = revocation.get("api_session")
    baseline = verify_pending_guidance_resume(
        before,
        before,
        before,
        tenant_id=tenant_id,
        product_id=product_id,
        project_id=project_id,
    )
    if baseline != pending:
        raise AssertionError(
            "Revocation baseline differs from the interrupted pending proposals"
        )
    for observation in (resumed, independent):
        if (
            not isinstance(observation, dict)
            or any(
                observation.get(field) != before[field]
                for field in ("id", "tenant_id", "product_id", "project_id", "revision")
            )
            or not isinstance(observation.get("proposals"), list)
        ):
            raise AssertionError(
                "Revocation readback changed scope or omitted pending proposals"
            )
    revoked = []
    safe = []
    for proposal in before["proposals"]:
        proofs = proposal.get("provenance")
        if (
            not isinstance(proofs, list)
            or not proofs
            or any(
                not isinstance(proof, dict)
                or not isinstance(proof.get("source_refs"), list)
                for proof in proofs
            )
        ):
            raise AssertionError("Revocation baseline lacks proposal provenance")
        (
            revoked if any(source in proof["source_refs"] for proof in proofs) else safe
        ).append(proposal)
    if (
        not revoked
        or resumed["proposals"] != safe
        or independent["proposals"] != safe
        or revocation.get("browser_visible_proposal_ids")
        != [proposal["id"] for proposal in safe]
    ):
        raise AssertionError(
            "Revoked or stale source proposals leaked on cross-client resume"
        )
    return [proposal["id"] for proposal in safe]


def capture_guidance_effects(
    tenant_id,
    actor_id,
    model_id,
    mode,
    read_usage,
    read_profile,
    read_session,
    read_provider,
    perform_turn,
    *,
    clock=time.monotonic,
    pause=time.sleep,
    deadline_seconds=30,
):
    if (
        mode not in ("model", "deterministic", "direct")
        or not all(
            callable(port)
            for port in (
                read_usage,
                read_profile,
                read_session,
                read_provider,
                perform_turn,
                clock,
                pause,
            )
        )
        or not 1 <= deadline_seconds <= 120
        or not all(
            isinstance(value, str) and value
            for value in (tenant_id, actor_id, model_id)
        )
    ):
        raise ValueError(
            "Guidance effect capture requires bounded, authorized observation ports"
        )

    def usage_snapshot():
        response = read_usage()
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("items"), list)
            or len(response["items"]) >= 100
            or response.get("total") != len(response["items"])
        ):
            raise ValueError("Usage reader must return a complete, bounded page")
        rows = response["items"]
        if any(
            not isinstance(row, dict)
            or row.get("org_id") != tenant_id
            or row.get("user_id") != actor_id
            or row.get("model") != model_id
            or not isinstance(row.get("request_id"), str)
            or not row["request_id"]
            or not isinstance(row.get("id"), str)
            or not row["id"]
            for row in rows
        ):
            raise ValueError(
                "Usage reader returned unscoped or unidentifiable metering"
            )
        by_id = {row["id"]: row for row in rows}
        if len(by_id) != len(rows) or len({row["request_id"] for row in rows}) != len(
            rows
        ):
            raise ValueError("Usage reader repeated a metered request")
        return by_id

    before = {
        "usage": usage_snapshot(),
        "profile": read_profile(),
        "session": read_session(),
        "provider": read_provider(),
    }
    if (
        any(
            not isinstance(before["profile"].get(field), str)
            or not before["profile"][field]
            or before["profile"].get(field) != before["session"].get(field)
            for field in ("product_id", "project_id")
        )
        or before["profile"].get("tenant_id") != tenant_id
        or before["session"].get("tenant_id") != tenant_id
        or not isinstance(before["provider"], dict)
    ):
        raise AssertionError("Metering fixture differs from the selected Product scope")
    result = perform_turn()
    if not isinstance(result, dict):
        raise AssertionError("Guide turn did not return a client observation")
    deadline = clock() + deadline_seconds
    after_usage = usage_snapshot()
    while clock() < deadline:
        pause(max(0, min(2, deadline - clock())))
        after_usage = usage_snapshot()
        if set(before["usage"]) - set(after_usage):
            raise AssertionError("Metering history changed during the guide turn")
        if len(set(after_usage) - set(before["usage"])) > 1:
            raise AssertionError("Guide turn exceeded its model-call bound")
    if any(after_usage.get(key) != value for key, value in before["usage"].items()):
        raise AssertionError("Metering history changed during the guide turn")
    new_usage = [
        after_usage[key] for key in after_usage.keys() - before["usage"].keys()
    ]
    after = {
        "usage": after_usage,
        "profile": read_profile(),
        "session": read_session(),
        "provider": read_provider(),
    }
    if any(
        after["profile"].get(field) != before["profile"].get(field)
        or after["session"].get(field) != before["session"].get(field)
        for field in ("tenant_id", "product_id", "project_id")
    ):
        raise AssertionError("Guide turn changed its Product scope")
    if after["provider"] != before["provider"]:
        raise AssertionError("Unselected external provider effects occurred")
    if mode == "direct":
        original = before["profile"]["product"]
        changed = after["profile"]["product"]
        if (
            new_usage
            or result != after["session"]
            or after["profile"]["revision"] != before["profile"]["revision"] + 1
            or after["session"]["revision"] != after["profile"]["revision"]
            or original.get("purpose") == changed.get("purpose")
            or {key: value for key, value in original.items() if key != "purpose"}
            != {key: value for key, value in changed.items() if key != "purpose"}
        ):
            raise AssertionError("Direct save must not invoke a model")
    elif mode == "deterministic":
        if (
            new_usage
            or result.get("mode") != "deterministic"
            or result.get("proposals") != []
        ):
            raise AssertionError(
                "Deterministic guidance must not invoke a model or propose effects"
            )
    else:
        if (
            len(new_usage) != 1
            or new_usage[0].get("status_code") != 200
            or result.get("mode") != "model"
        ):
            raise AssertionError(
                "Model guidance needs exactly one successful metered call"
            )
        proposals = result.get("proposals")
        if (
            not isinstance(proposals, list)
            or len(proposals) > 10
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"]
                for item in proposals
            )
            or len({item["id"] for item in proposals}) != len(proposals)
        ):
            raise AssertionError("Model returned unbounded or repeated proposals")
        if any(
            set(item) != MODEL_PROPOSAL_FIELDS
            or not isinstance(item.get("product"), dict)
            or not item["product"]
            or not set(item["product"]) <= ALLOWED_PROPOSAL_FIELDS
            or item.get("configuration") != {}
            or item.get("remaining_paths")
            != [f"/product/{field}" for field in item["product"]]
            or item.get("base_revision") != before["profile"]["revision"]
            or not isinstance(item.get("expires_at"), str)
            or not item["expires_at"].endswith("Z")
            or not isinstance(item.get("provenance"), list)
            or len(item["provenance"]) != 1
            or not isinstance(item["provenance"][0], dict)
            or set(item["provenance"][0]) != MODEL_PROVENANCE_FIELDS
            or item["provenance"][0]["paths"] != item["remaining_paths"]
            or item["provenance"][0]["state"] != "missing"
            or item["provenance"][0]["method"] != "extracted"
            or item["provenance"][0]["source_refs"] != []
            or not isinstance(item["provenance"][0]["observed_at"], str)
            or not item["provenance"][0]["observed_at"].endswith("Z")
            for item in proposals
        ):
            raise AssertionError("Model proposal escaped reviewable Product fields")
        stored = after["session"].get("proposals", [])
        if not all(item in stored for item in proposals):
            raise AssertionError(
                "Selected proposals differ from independent session readback"
            )
    if mode != "direct" and (
        after["profile"] != before["profile"]
        or after["session"]["revision"] != before["session"]["revision"]
    ):
        raise AssertionError("Guidance silently edited the Product")
    return {
        "mode": mode,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "model_id": model_id,
        "metered_requests": len(new_usage),
        "request_ids": [row["request_id"] for row in new_usage],
        "usage_row_ids_before": sorted(before["usage"]),
        "usage_row_ids_after": sorted(after_usage),
        "proposal_ids": [item["id"] for item in result.get("proposals", [])]
        if mode != "direct"
        else [],
        "profile_revision_before": before["profile"]["revision"],
        "profile_revision_after": after["profile"]["revision"],
        "provider_before": before["provider"],
        "provider_after": after["provider"],
    }


def guide_with_installed_cli(
    binary, checksums, fixture, base_url, session, record, mode="deterministic"
):
    from tests.e2e.new_ui.test_product_resume import installed_cli

    if mode not in ("deterministic", "model"):
        raise ValueError("Installed guidance mode must be explicit and bounded")
    scope = [
        "--product",
        fixture["product_id"],
        "--project",
        fixture["project_id"],
        "--session",
        session["id"],
        "--revision",
        str(session["revision"]),
    ]
    return installed_cli(
        binary,
        checksums,
        fixture["owner"],
        base_url,
        [
            "guide",
            *scope,
            "--idempotency-key",
            "guidance_turn_" + mode + "_" + fixture["run_id"].replace("-", "_"),
            "--input",
            "-",
            "--mode",
            mode,
        ],
        input_body={"message": "What is missing?"},
        record=record,
    )
