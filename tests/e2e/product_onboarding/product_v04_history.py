"""Durable, installed-client binding history against a run-owned Product."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import quote

from tests.e2e.new_ui.test_product_resume import installed_cli
from tests.e2e.new_ui.test_product_scopes import (
    assert_scope_fixture_inputs,
    assert_scope_profile,
)
from tests.e2e.product_onboarding.product_v02_record import private_json
from tests.e2e.product_onboarding.product_v04_cli import install_served_cli
from tests.e2e.product_onboarding.product_v04_evidence import HISTORY_SELECTORS


def route(claim):
    return (
        "/products/"
        + quote(claim["product_id"], safe="")
        + "/projects/"
        + quote(claim["project_id"], safe="")
    )


def observe(provider, claim, path, status, observations):
    response = provider.request.get(
        provider.base_url + "/api/v1" + path,
        headers={"Authorization": "Bearer " + provider.owner["access_token"]},
        timeout=30_000,
    )
    if response.status != status:
        raise AssertionError(
            "Owned Project history/readback differs from the original mutation"
        )
    result = response.json()
    observations.append({"status": status, "body": result})
    return result


def history(provider, claim, observations):
    result = observe(
        provider, claim, route(claim) + "/project-bindings", 200, observations
    )
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise AssertionError("Project binding history is not a complete API record")
    return result["items"]


def timestamp(value):
    if not isinstance(value, str):
        raise AssertionError("Project binding is missing a dated revision")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AssertionError("Project binding has an invalid dated revision") from None
    if result.tzinfo is None:
        raise AssertionError("Project binding has no timezone")
    return result


def version(row, claim):
    versions = row.get("relationship_versions") if isinstance(row, dict) else None
    if not isinstance(versions, list) or len(versions) != 1:
        raise AssertionError("Project binding has no exact relationship version")
    value = versions[0]
    if (
        not isinstance(value, dict)
        or value.get("from_ref", {}).get("record_id") != claim["product_id"]
        or value.get("to_ref", {}).get("record_id") != claim["project_id"]
        or value.get("relationship_type") != "product_project"
    ):
        raise AssertionError("Project binding belongs to another Product or Project")
    return value


def journal(provider, fixture, observations):
    scope = provider.state.document.get("scope")
    first, second = assert_scope_fixture_inputs(fixture)
    if (
        not isinstance(scope, dict)
        or scope.get("cleanup_pending") is not True
        or scope.get("product", {}).get("product_id") != fixture["product_id"]
        or scope.get("product", {}).get("project_id") != first["project_id"]
        or scope.get("receipts", {}).get("second_project", {}).get("project_id")
        != second["project_id"]
        or provider.owner["actor_id"] != fixture["actor_id"]
        or provider.owner["tenant_id"] != fixture["tenant_id"]
    ):
        raise ValueError(
            "History mutation requires the original run-owned two-scope fixture"
        )
    prior = scope.get("cli_history")
    if prior is not None:
        if (
            prior["run_id"] != fixture["run_id"]
            or prior["product_id"] != fixture["product_id"]
            or prior["project_id"] != second["project_id"]
            or prior["first_id"] != first["project_id"]
            or prior["project_name"] != second["name"]
        ):
            raise ValueError("History replay differs from its original durable claim")
        return prior
    claim = {
        "run_id": fixture["run_id"],
        "product_id": fixture["product_id"],
        "project_id": second["project_id"],
        "first_id": first["project_id"],
        "project_name": second["name"],
    }
    original = observe(provider, claim, route(claim), 200, observations)
    assert_scope_profile(original, fixture, second)
    initial = history(provider, claim, observations)
    if (
        not initial
        or len(initial) > 100
        or type(original.get("revision")) is not int
        or original["revision"] <= 0
    ):
        raise AssertionError("Original dated Project binding is missing or unbounded")
    latest = version(initial[-1], claim)
    if (
        latest.get("valid_to") is not None
        or not latest.get("relationship_id")
        or not latest.get("relationship_revision")
        or initial[-1].get("revision") > original["revision"]
    ):
        raise AssertionError(
            "Original Project relationship has no active exact revision"
        )
    timestamp(latest["valid_from"])
    claim.update(
        before=deepcopy(initial),
        profile=original,
        revision=original["revision"],
        relationship=latest,
        unbind_key="scope_cli_history_unbind_" + fixture["run_id"],
        rebind_key="scope_cli_history_rebind_" + fixture["run_id"],
        status="pending",
    )
    updated = deepcopy(scope)
    updated["cli_history"] = claim
    provider.state.save({**provider.state.document, "scope": updated})
    return claim


def project_asset(provider, claim, bound, observations):
    listing = observe(provider, claim, "/projects", 200, observations)
    if (
        not isinstance(listing, dict)
        or not isinstance(listing.get("items"), list)
        or len(listing["items"]) > 100
        or listing.get("next_cursor") is not None
        or len(
            [item for item in listing["items"] if item.get("id") == claim["project_id"]]
        )
        != 1
    ):
        raise AssertionError(
            "Original owned Project asset is not independently discoverable"
        )
    asset = next(item for item in listing["items"] if item["id"] == claim["project_id"])
    if (
        asset.get("project_id", claim["project_id"]) != claim["project_id"]
        or asset.get("name") != claim["project_name"]
        or asset.get("product_id") != (claim["product_id"] if bound else None)
    ):
        raise AssertionError("Unlink removed or rebound the original Project asset")


def closed(provider, claim, observations):
    rows = history(provider, claim, observations)
    if (
        len(rows) != len(claim["before"]) + 1
        or rows[:-1] != claim["before"]
        or rows[-1].get("revision") != claim["revision"] + 1
    ):
        raise AssertionError("Unlink failed to preserve exact original binding history")
    prior, current = claim["relationship"], version(rows[-1], claim)
    if (
        current.get("relationship_id") != prior["relationship_id"]
        or current.get("valid_from") != prior["valid_from"]
        or current.get("supersedes_revision") != prior["relationship_revision"]
        or timestamp(current.get("valid_to")) <= timestamp(prior["valid_from"])
    ):
        raise AssertionError("Unlink did not close the original dated relationship")
    observe(provider, claim, route(claim), 404, observations)
    first = observe(
        provider,
        claim,
        route(claim).rsplit("/", 1)[0] + "/" + quote(claim["first_id"], safe=""),
        200,
        observations,
    )
    if (
        first.get("revision") != claim["revision"] + 1
        or first.get("product", {}).get("owner_ref")
        != claim["profile"]["product"]["owner_ref"]
    ):
        raise AssertionError("Unlink changed the shared Product or another scope")
    project_asset(provider, claim, False, observations)
    return rows


def restored(provider, claim, observations):
    rows = history(provider, claim, observations)
    if (
        len(rows) != len(claim["before"]) + 2
        or rows[: len(claim["before"])] != claim["before"]
        or rows[-2].get("revision") != claim["revision"] + 1
        or rows[-1].get("revision") != claim["revision"] + 2
    ):
        raise AssertionError("Rebind changed the original relationship history")
    old, ended, new = (
        claim["relationship"],
        version(rows[-2], claim),
        version(rows[-1], claim),
    )
    if (
        ended.get("relationship_id") != old["relationship_id"]
        or ended.get("valid_from") != old["valid_from"]
        or ended.get("supersedes_revision") != old["relationship_revision"]
        or timestamp(ended.get("valid_to")) <= timestamp(old["valid_from"])
        or new.get("relationship_id") in (None, old["relationship_id"])
        or new.get("relationship_revision") != "1"
        or "valid_to" in new
        or timestamp(new.get("valid_from")) < timestamp(ended["valid_to"])
    ):
        raise AssertionError("Rebind changed or lost dated Project history")
    profile = observe(provider, claim, route(claim), 200, observations)
    before = claim["profile"]
    if (
        profile.get("revision") != claim["revision"] + 2
        or profile.get("product", {}).get("owner_ref") != before["product"]["owner_ref"]
        or profile.get("configuration") != before["configuration"]
        or profile.get("progress") != before["progress"]
        or profile.get("project") != before["project"]
    ):
        raise AssertionError(
            "Rebind replaced the original Project configuration or progress"
        )
    project_asset(provider, claim, True, observations)
    return rows


def restore_history(provider):
    claim = provider.state.document.get("scope", {}).get("cli_history")
    if claim is None:
        return
    scope = provider.state.document["scope"]
    if (
        claim.get("run_id") != provider.state.binding["run_id"]
        or scope.get("cleanup_pending") is not True
        or claim.get("product_id") != scope.get("product", {}).get("product_id")
        or claim.get("first_id") != scope.get("product", {}).get("project_id")
        or claim.get("project_id")
        != scope.get("receipts", {}).get("second_project", {}).get("project_id")
    ):
        raise ValueError("History compensation cannot act on an unowned fixture")
    reads = []
    rows = history(provider, claim, reads)
    if rows == claim["before"]:
        profile = observe(provider, claim, route(claim), 200, reads)
        if profile != claim["profile"]:
            raise AssertionError("Original Project changed before history compensation")
        status = "not_mutated"
    else:
        if len(rows) == len(claim["before"]) + 1:
            closed(provider, claim, reads)
            response = None
            try:
                response = provider.request.post(
                    provider.base_url
                    + "/api/v1/products/"
                    + quote(claim["product_id"], safe="")
                    + "/projects",
                    headers={
                        "Authorization": "Bearer " + provider.owner["access_token"],
                        "Idempotency-Key": claim["rebind_key"],
                        "If-Profile-Revision": str(claim["revision"] + 1),
                    },
                    data={"mode": "select", "project_id": claim["project_id"]},
                    timeout=30_000,
                )
            except (ConnectionError, TimeoutError):
                pass
            if response is not None and response.status != 200:
                try:
                    restored(provider, claim, reads)
                except AssertionError:
                    raise AssertionError(
                        "Original Project compensation was refused"
                    ) from None
        restored(provider, claim, reads)
        status = "restored"
    updated = deepcopy(provider.state.document["scope"])
    updated["cli_history"]["status"] = status
    provider.state.save({**provider.state.document, "scope": updated})


def run_history(provider, fixture, evidence):
    first, second = assert_scope_fixture_inputs(fixture)
    if (
        fixture["run_id"] != evidence.run_id
        or fixture["source_revision"] != evidence.source
        or evidence.context["identities"]["source_revision"] != evidence.source
        or any(
            evidence.cli_attempts[case][0]["result"] != "pass"
            for case in ("AC-01", "AC-02")
        )
    ):
        raise ValueError(
            "History requires the same passed protected CLI attempt and signed release"
        )
    events = SimpleNamespace(cli_events=[])
    reads, result, checksums = [], "interrupted", {}
    claim = None
    attempts = len(evidence.cli_attempts.get("AC-03", [])) + 1
    try:
        claim = journal(provider, fixture, reads)
        if claim["status"] not in ("pending", "not_mutated"):
            raise ValueError(
                "Original history claim was already compensated; do not repeat mutations"
            )
        with TemporaryDirectory(prefix="product-v04-cli-history-") as home:
            binary, checksums = install_served_cli(provider, evidence, Path(home))
            arguments = [
                "--product",
                fixture["product_id"],
                "--project",
                second["project_id"],
            ]

            def command(parts):
                return installed_cli(
                    binary,
                    checksums,
                    provider.owner,
                    provider.base_url,
                    parts,
                    record=events,
                    command="product",
                )

            def bindings(expected):
                observed = command(["project", "bindings", *arguments])
                if observed != {"items": expected}:
                    raise AssertionError(
                        "Installed CLI history differs from independent API"
                    )

            bindings(claim["before"])
            try:
                command(
                    [
                        "project",
                        "unbind",
                        *arguments,
                        "--yes",
                        "--revision",
                        str(claim["revision"]),
                        "--idempotency-key",
                        claim["unbind_key"],
                    ]
                )
            except AssertionError:
                closed(provider, claim, reads)
            ended = closed(provider, claim, reads)
            bindings(ended)
            input_file = Path(home) / "rebind.json"
            private_json(
                input_file, {"mode": "select", "project_id": second["project_id"]}
            )
            try:
                command(
                    [
                        "project",
                        "bind",
                        "--product",
                        fixture["product_id"],
                        "--input",
                        str(input_file),
                        "--revision",
                        str(claim["revision"] + 1),
                        "--idempotency-key",
                        claim["rebind_key"],
                    ]
                )
            except AssertionError:
                restored(provider, claim, reads)
            renewed = restored(provider, claim, reads)
            bindings(renewed)
            final = observe(
                provider,
                claim,
                route(claim).rsplit("/", 1)[0]
                + "/"
                + quote(first["project_id"], safe=""),
                200,
                reads,
            )
            assert_scope_profile(final, fixture, first)
            if final["revision"] != claim["revision"] + 2:
                raise AssertionError("Rebind changed the first delivery scope")
            result = "pass"
    finally:
        try:
            if claim is not None:
                restore_history(provider)
        except Exception:
            result = "interrupted"
            raise
        finally:
            evidence.cli_attempts.setdefault("AC-03", [])
            evidence.api_attempts = getattr(evidence, "api_attempts", {})
            artifact = private_json(
                evidence.root / f"ac-03-cli-result-{attempts}.json",
                {"events": events.cli_events, "result": result},
            )
            readback = private_json(
                evidence.root / f"ac-03-cli-api-{attempts}.json", reads
            )
            base = {
                "run_id": evidence.run_id,
                "requirements_revision": evidence.context["identities"][
                    "requirements_revision"
                ],
                "source_revision": evidence.source,
                "workflow_run_id": evidence.run,
                "workflow_attempt": evidence.attempt,
                "result": result,
                "expected": "Dated unlink/rebind preserves ownership, scope data and history",
                "actual": "Installed history and independent API "
                + ("matched" if result == "pass" else "did not complete"),
                "artifact_ref": artifact,
                "readback_ref": readback,
            }
            evidence.cli_attempts["AC-03"].append(
                {
                    **base,
                    "attempt_id": f"{evidence.run_id}/AC-03/cli/{attempts}",
                    "surface": "cli",
                    "test_ref": HISTORY_SELECTORS["cli"],
                    "stdout_ref": private_json(
                        evidence.root / f"ac-03-cli-stdout-{attempts}.json",
                        [event["stdout"] for event in events.cli_events],
                    ),
                    "installed_cli_sha256": checksums.get("adp"),
                    "exit_code": 0 if result == "pass" else 1,
                }
            )
            evidence.api_attempts.setdefault("AC-03", []).append(
                {
                    **base,
                    "attempt_id": f"{evidence.run_id}/AC-03/api/{attempts}",
                    "surface": "api",
                    "test_ref": HISTORY_SELECTORS["api"],
                    "artifact_ref": private_json(
                        evidence.root / f"ac-03-api-result-{attempts}.json",
                        {"result": result, "observations": len(reads)},
                    ),
                    "readback_ref": readback,
                }
            )
