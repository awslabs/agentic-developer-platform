"""Deployed guidance producers for issue #8258 AC-02 and AC-03.

AC-02 measures what one deterministic, one model and one direct guidance turn
actually do: the installed CLI and the served API perform the turns while an
independent authorized usage journal, the Product profile and the authorized
source projection are read back around every turn.

AC-03 proves cross-client resume of pending, source-backed guidance. A
connected financial document source is selected, imported through the installed
CLI, observed in the served browser and the API, kept intact across a
deterministic (model outage) turn, and then revoked; after revocation the
proposals that depended on that source must disappear from every client.

Both producers only observe the deployment. The evidence reports are written by
the maintained new-UI adapters, so the producers and issue #8273's report
validators share one contract.
"""

import json
import os
import re
import time
from urllib.parse import quote, urlencode

from tests.e2e.product_onboarding.product_v06_live import api_call, api_json, project_path

DOCUMENT_PATH = "docs/product-onboarding/examples/financial-plan.json"
RECOVERY_ATTEMPTS = ("proposal", "interruption", "resume", "outage", "revocation", "cleanup")
USAGE_DEADLINE_SECONDS = 30
SOURCE_PAGE_BUDGET = 5
PREREQUISITES = {
    "model": (
        "The deployed onboarding guidance model is not admitted for this environment:"
        " one authorized model turn produced no metered model call"
    ),
    "document": (
        "The pinned regression repository is not an authorized GitHub source of the"
        " evaluation workspace, so no financial document source can be selected"
    ),
    "source_ref": (
        "The deployed financial import published no authorized source reference with"
        " its pending proposals"
    ),
}


class GuidancePrerequisite(Exception):
    """A deployed capability the selected guidance cases cannot provision."""


def prerequisite(name):
    raise GuidancePrerequisite(PREREQUISITES[name])


def installed_cli_release():
    for variable in ("PRODUCT_RESUME_CLI_PATH", "PRODUCT_RESUME_CLI_HASHES"):
        if not os.environ.get(variable):
            raise ValueError("The deployed guidance producer needs " + variable + " from its suite provider")
    return os.environ["PRODUCT_RESUME_CLI_PATH"], json.loads(os.environ["PRODUCT_RESUME_CLI_HASHES"])


def guidance_key(run_id, suffix):
    key = "guidance_" + suffix + "_" + run_id.replace("-", "_")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
        raise ValueError("The deployed guidance producer built an unusable idempotency key")
    return key


def scope_url(base_url, fixture, suffix=""):
    return base_url + project_path(fixture) + suffix


def session_url(base_url, fixture, session_id, suffix=""):
    return scope_url(base_url, fixture, "/onboarding-sessions/" + quote(session_id, safe="") + suffix)


def profile_revision(provider, fixture, base_url):
    revision = api_json(provider.request, fixture["owner"], "get", scope_url(base_url, fixture))["revision"]
    if not isinstance(revision, int) or revision < 1:
        raise AssertionError("The deployed Product profile answered without a usable revision")
    return revision


def cli_arguments(fixture, action, session_id=None, *, extra=()):
    arguments = [action, "--product", fixture["product_id"], "--project", fixture["project_id"]]
    if session_id is not None:
        arguments += ["--session", session_id]
    return [*arguments, *extra]


def metered_rows(provider, fixture, base_url, *, model_id=None):
    """Read the independent authorized usage journal for this fixture actor."""
    query = {"org_id": fixture["tenant_id"], "user_id": fixture["actor_id"], "limit": 100}
    if model_id:
        query["model"] = model_id
    response = provider.request.get(
        base_url + "/api/usage/logs?" + urlencode(query),
        headers={"Authorization": "Bearer " + provider.usage_control["access_token"]},
        timeout=30_000,
    )
    if response.status != 200:
        raise ValueError("Independent authorized usage readback is unavailable")
    payload = response.json()
    rows = payload.get("items")
    if not isinstance(rows, list) or len(rows) >= 100 or payload.get("total") != len(rows):
        raise ValueError("Usage reader must return a complete, bounded page")
    identified = {
        row["id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    }
    if len(identified) != len(rows):
        raise ValueError("Usage reader returned unidentifiable metering")
    return identified


def new_metered_rows(before, after):
    if set(before) - set(after):
        raise AssertionError("Metering history changed during the observed guidance turn")
    return [after[key] for key in sorted(set(after) - set(before))]


def external_snapshot(provider, fixture, base_url):
    """A stable projection of the authorized external sources of this Project."""
    sources = api_json(provider.request, fixture["owner"], "get", scope_url(base_url, fixture, "/sources?limit=50"))
    bindings = api_json(provider.request, fixture["owner"], "get", scope_url(base_url, fixture, "/bindings?limit=50"))
    return {
        "connections": sorted(
            json.dumps(item.get("connection_ref"), sort_keys=True) for item in sources.get("items") or []
        ),
        "bindings": sorted(
            item["id"] for item in bindings.get("items") or [] if item.get("status") != "unlinked"
        ),
    }


def discovered_model_id(provider, fixture, base_url, session, *, pause=time.sleep):
    """Learn the admitted guidance model from one authorized metered discovery turn.

    The deployed guidance response never discloses the configured model, so the
    only authorized observation is the metered call the turn itself produces.
    """
    before = metered_rows(provider, fixture, base_url)
    payload, _ = api_call(
        provider.request, fixture["owner"], "post",
        session_url(base_url, fixture, session["id"], "/guidance"),
        body={"mode": "model", "message": "What is missing?"},
        headers={
            "Idempotency-Key": guidance_key(fixture["run_id"], "discover"),
            "If-Profile-Revision": str(session["revision"]),
        },
        expected=(200, 403),
    )
    if payload.get("mode") != "model":
        prerequisite("model")
    rows = []
    deadline = time.monotonic() + USAGE_DEADLINE_SECONDS
    while not rows and time.monotonic() < deadline:
        pause(2)
        rows = new_metered_rows(before, metered_rows(provider, fixture, base_url))
    if len(rows) != 1 or not isinstance(rows[0].get("model"), str) or not rows[0]["model"]:
        prerequisite("model")
    return rows[0]["model"]


def repository_resource(provider, fixture, base_url, connection_id, repository_id):
    cursor = None
    for _ in range(SOURCE_PAGE_BUDGET):
        query = {"limit": 50, "connection_kind": "github_installation", "connection_id": connection_id}
        if cursor is not None:
            query["resource_cursor"] = cursor
        listing = api_json(
            provider.request, fixture["owner"], "get",
            scope_url(base_url, fixture, "/sources?" + urlencode(query)),
        )
        for source in listing.get("items") or []:
            for resource in source.get("resources") or []:
                if resource.get("kind") == "repository" and resource.get("native_id") == repository_id:
                    return resource
            cursor = source.get("resource_cursor")
        if cursor is None:
            return None
    return None


def document_binding(provider, fixture, base_url):
    """Select the committed financial document of the pinned regression repository."""
    repository_id = os.environ["GITHUB_REPOSITORY_ID"]
    listing = api_json(provider.request, fixture["owner"], "get", scope_url(base_url, fixture, "/sources?limit=50"))
    for item in listing.get("items") or []:
        reference = item.get("connection_ref") or {}
        if reference.get("kind") != "github_installation" or item.get("provider") != "github":
            continue
        resource = repository_resource(provider, fixture, base_url, reference["id"], repository_id)
        if resource is None:
            continue
        native_id = repository_id + ":" + DOCUMENT_PATH
        view = api_json(
            provider.request, fixture["owner"], "post", scope_url(base_url, fixture, "/bindings"),
            body={
                "kind": "document",
                "provider": "github",
                "connection_ref": {"kind": "github_installation", "id": reference["id"]},
                "namespace": resource["namespace"],
                "native_id": native_id,
            },
            headers={
                "Idempotency-Key": guidance_key(fixture["run_id"], "document"),
                "If-Profile-Revision": str(profile_revision(provider, fixture, base_url)),
            },
            expected=(200, 201),
        )
        if not isinstance(view.get("id"), str) or not view["id"]:
            raise AssertionError("The deployed source selection returned no binding identity")
        return {"id": view["id"], "native_id": native_id}
    prerequisite("document")


def document_source_ref(session, native_id):
    for proposal in session.get("proposals") or []:
        for proof in proposal.get("provenance") or []:
            for reference in proof.get("source_refs") or []:
                if (
                    isinstance(reference, dict)
                    and reference.get("record_type") == "document"
                    and reference.get("record_id") == native_id
                ):
                    return reference
    prerequisite("source_ref")


def open_guidance_panel(page, fixture, base_url):
    """Resume the Product onboarding session in the served browser."""
    from playwright.sync_api import expect

    from tests.e2e.chat.helpers import inject_tokens_and_navigate
    from tests.e2e.new_ui.product_navigation import open_product_section, product_session_status

    route = (
        "/products/" + quote(fixture["product_id"], safe="")
        + "/projects/" + quote(fixture["project_id"], safe="")
    )
    inject_tokens_and_navigate(page, fixture["owner"], path=route)
    open_product_section(page, "Financials")
    start = page.get_by_role("button", name="Start or resume onboarding")
    if start.count():
        start.click()
    expect(product_session_status(page)).to_contain_text("Session ")
    return re.search(r"Session ([^ ]+) · Revision", product_session_status(page).inner_text()).group(1)


def browser_visible_proposals(page, proposal_ids):
    return [
        proposal_id
        for proposal_id in proposal_ids
        if page.get_by_role("button", name="Accept suggested date from " + proposal_id).count()
    ]


def run_guidance_effects(provider, fixture, base_url, record, directory):
    """Issue #8258 AC-02: measure the effects of every client guidance turn."""
    from tests.e2e.cli_uplift.product_guidance import guide_with_installed_cli
    from tests.e2e.new_ui.test_product_guidance import (
        capture_client_effect_turn,
        save_direct_with_installed_cli,
        write_client_effect_report,
    )
    from tests.e2e.new_ui.test_product_resume import installed_cli, read_session
    from tests.e2e.product_onboarding.test_v07 import GUIDANCE_TURN_ORDER

    binary, checksums = installed_cli_release()
    session = installed_cli(
        binary, checksums, fixture["owner"], base_url,
        cli_arguments(fixture, "start", extra=(
            "--revision", str(profile_revision(provider, fixture, base_url)),
            "--idempotency-key", guidance_key(fixture["run_id"], "start"),
        )),
        record=record,
    )
    session_id = session["id"]
    model_id = discovered_model_id(
        provider, fixture, base_url, read_session(provider, base_url, fixture, session_id)
    )
    turns = {}
    for mode in GUIDANCE_TURN_ORDER:
        def perform(mode=mode):
            current = read_session(provider, base_url, fixture, session_id)
            if mode == "direct":
                return save_direct_with_installed_cli(binary, checksums, fixture, base_url, current, record)
            return guide_with_installed_cli(binary, checksums, fixture, base_url, current, record, mode=mode)

        turns[mode] = capture_client_effect_turn(
            fixture, base_url, provider, session_id, model_id, mode,
            lambda: external_snapshot(provider, fixture, base_url), perform,
        )
    return {
        "session_id": session_id,
        "model_id": model_id,
        "report": write_client_effect_report(fixture, turns, str(directory)),
    }


def pending_observations(provider, fixture, base_url, record, session_id, *, binary, checksums):
    """One API, one installed-CLI and one independent API observation of the session."""
    from tests.e2e.new_ui.test_product_resume import installed_cli, read_session

    first = read_session(provider, base_url, fixture, session_id)
    resumed = installed_cli(
        binary, checksums, fixture["owner"], base_url,
        cli_arguments(fixture, "resume", session_id), record=record,
    )
    independent = read_session(provider, base_url, fixture, session_id)
    return first, resumed, independent


def run_guidance_resume(provider, page, fixture, base_url, record):
    """Issue #8258 AC-03: cross-client resume and source revocation of guidance."""
    from tests.e2e.cli_uplift.product_guidance import (
        guide_with_installed_cli,
        verify_pending_guidance_resume,
    )
    from tests.e2e.new_ui.test_product_resume import installed_cli, read_session

    binary, checksums = installed_cli_release()
    scope = {
        "tenant_id": fixture["tenant_id"],
        "product_id": fixture["product_id"],
        "project_id": fixture["project_id"],
    }
    binding = document_binding(provider, fixture, base_url)
    session_id = open_guidance_panel(page, fixture, base_url)
    imported = installed_cli(
        binary, checksums, fixture["owner"], base_url,
        cli_arguments(fixture, "guide", session_id, extra=(
            "--revision", str(profile_revision(provider, fixture, base_url)),
            "--idempotency-key", guidance_key(fixture["run_id"], "financial"),
            "--mode", "financial", "--input", "-",
        )),
        input_body={"message": "Import the connected financial document"}, record=record,
    )
    if imported.get("mode") != "financial" or not imported.get("proposals"):
        raise AssertionError("The deployed financial import published no reviewable proposals")
    observations = pending_observations(
        provider, fixture, base_url, record, session_id, binary=binary, checksums=checksums
    )
    pending = verify_pending_guidance_resume(*observations, **scope)
    before_session = observations[0]
    source_ref = document_source_ref(before_session, binding["native_id"])
    before_rows = metered_rows(provider, fixture, base_url)
    deterministic = guide_with_installed_cli(binary, checksums, fixture, base_url, before_session, record)
    metered = new_metered_rows(before_rows, metered_rows(provider, fixture, base_url))
    if deterministic.get("mode") != "deterministic" or deterministic.get("proposals") != []:
        raise AssertionError("A model outage must leave deterministic guidance without proposals")
    if metered:
        raise AssertionError("Deterministic guidance metered a model call during the outage")
    outage = {
        "mode": deterministic["mode"],
        "metered_requests": len(metered),
        "proposal_ids": [item["id"] for item in deterministic["proposals"]],
        "pending_after": verify_pending_guidance_resume(
            *pending_observations(
                provider, fixture, base_url, record, session_id, binary=binary, checksums=checksums
            ),
            **scope,
        ),
    }
    tombstone = api_json(
        provider.request, fixture["owner"], "delete",
        scope_url(base_url, fixture, "/bindings/" + quote(binding["id"], safe="")),
        headers={
            "Idempotency-Key": guidance_key(fixture["run_id"], "revoke"),
            "If-Profile-Revision": str(profile_revision(provider, fixture, base_url)),
        },
        expected=(200,),
    )
    if tombstone.get("status") != "unlinked":
        raise AssertionError("Revoking the financial document source left it linked")
    revocation = {
        "source_ref": source_ref,
        "before_session": before_session,
        "cli_session": installed_cli(
            binary, checksums, fixture["owner"], base_url,
            cli_arguments(fixture, "resume", session_id), record=record,
        ),
        "api_session": read_session(provider, base_url, fixture, session_id),
    }
    if open_guidance_panel(page, fixture, base_url) != session_id:
        raise AssertionError("The served browser resumed a different onboarding session")
    revocation["browser_visible_proposal_ids"] = browser_visible_proposals(page, pending["proposal_ids"])
    attempts = [
        {"attempt_id": fixture["run_id"] + "/" + stage, "stage": stage, "result": "pass"}
        for stage in RECOVERY_ATTEMPTS
    ]
    return pending, outage, revocation, attempts
