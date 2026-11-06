"""API driver for independently verified administrator handoff acceptance."""

import re
import time
from urllib.parse import quote


def _read(request, url, token):
    response = request.get(
        url, headers={"Authorization": "Bearer " + token}, timeout=30_000
    )
    if response.status != 200:
        raise AssertionError(f"Handoff readback returned {response.status}")
    return response.json()


def _no_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(
                r"token|secret|credential|password|authorization", key, re.IGNORECASE
            ):
                raise AssertionError("Handoff readback disclosed credentials")
            _no_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _no_secrets(item)


def start_admin_handoff(request, base_url, fixture, session_id, key):
    """Start one durable operation; the existing admin workflow owns the effect."""
    if (
        not session_id
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key)
        or not all(
            fixture.get(name)
            for name in ("run_id", "tenant_id", "actor_id", "product_id", "project_id")
        )
    ):
        raise ValueError(
            "Handoff requires owned scope, session and a stable idempotency key"
        )
    owner = fixture["owner"]["access_token"]
    root = (
        base_url.rstrip("/")
        + "/api/v1/products/"
        + quote(fixture["product_id"], safe="")
        + "/projects/"
        + quote(fixture["project_id"], safe="")
    )
    session_url = root + "/onboarding-sessions/" + quote(session_id, safe="")
    before = _read(request, session_url, owner)
    if (
        before.get("id") != session_id
        or before.get("tenant_id") != fixture["tenant_id"]
        or before.get("state") != "open"
    ):
        raise AssertionError("Handoff must start in the owned, open session")
    response = request.post(
        session_url + "/handoffs",
        data={"target": "administrator", "action": "assign_team"},
        headers={
            "Authorization": "Bearer " + owner,
            "Idempotency-Key": key,
            "If-Profile-Revision": str(before["revision"]),
        },
        timeout=30_000,
    )
    if response.status not in (200, 202):
        raise AssertionError(f"Handoff start returned {response.status}")
    operation = response.json()
    if (
        not operation.get("id")
        or operation.get("state") != "waiting_external"
        or any(
            operation.get(name) != fixture[name]
            for name in ("tenant_id", "product_id", "project_id")
        )
        or operation.get("resume_path") != before.get("resume_path")
    ):
        raise AssertionError(
            "Handoff started outside the owned session or did not wait"
        )
    _no_secrets(operation)
    return {
        "before": before,
        "operation_id": operation["id"],
        "session_url": session_url,
        "operation_url": root + "/operations/" + quote(operation["id"], safe=""),
    }


def finish_admin_handoff(
    request, fixture, started, receipt_observer, *, timeout=30, interval=1
):
    """Require completion, unchanged unrelated work and an actual callback receipt."""
    if timeout <= 0 or interval <= 0:
        raise ValueError("Handoff polling must be bounded")
    owner = fixture["owner"]["access_token"]
    deadline = time.monotonic() + timeout
    while True:
        operation = _read(request, started["operation_url"], owner)
        _no_secrets(operation)
        if operation.get("id") != started["operation_id"] or any(
            operation.get(name) != fixture[name]
            for name in ("tenant_id", "product_id", "project_id")
        ):
            raise AssertionError("Resumed handoff changed operation or scope")
        if operation.get("state") == "succeeded":
            break
        if operation.get("state") != "waiting_external":
            raise AssertionError("Handoff did not complete successfully")
        if time.monotonic() >= deadline:
            raise AssertionError(
                "Handoff callback did not arrive within the polling bound"
            )
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    receipt = receipt_observer(started["operation_id"])
    if (
        not isinstance(receipt, dict)
        or receipt.get("operation_id") != started["operation_id"]
        or receipt.get("tenant_id") != fixture["tenant_id"]
        or receipt.get("status") != "succeeded"
        or not receipt.get("provider_operation_id")
        or type(receipt.get("sequence")) is not int
        or receipt["sequence"] < 1
    ):
        raise AssertionError("Matching provider callback receipt was not observed")
    _no_secrets(receipt)
    after = _read(request, started["session_url"], owner)
    _no_secrets(after)
    if (
        after.get("id") != started["before"]["id"]
        or after.get("state") != "open"
        or started["operation_id"] in after.get("operation_ids", [])
        or after.get("steps", {}).get("financials")
        != started["before"].get("steps", {}).get("financials")
    ):
        raise AssertionError(
            "Handoff lost unrelated session progress or remained pending"
        )
    return {"operation": operation, "session": after, "callback_receipt": receipt}


def recover_repository(
    request,
    base_url,
    fixture,
    session_id,
    plan,
    keys,
    provider_observer,
    *,
    browser_preview=None,
    browser_confirm=None,
):
    """Confirm once, replay only the same key, and inspect the provider outcome."""
    from_browser = browser_preview is not None or browser_confirm is not None
    if (
        from_browser
        and not (callable(browser_preview) and callable(browser_confirm))
        or not session_id
        or not isinstance(plan, dict)
        or not isinstance(keys, dict)
        or set(keys)
        != ({"reconcile"} if from_browser else {"preview", "confirm", "reconcile"})
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value)
            for value in keys.values()
        )
        or len(set(keys.values())) != len(keys)
        or not all(
            fixture.get(name)
            for name in (
                "run_id",
                "tenant_id",
                "actor_id",
                "product_id",
                "project_id",
                "owned_repository_name",
                "owner_native_id",
                "expected_protections",
            )
        )
        or plan.get("confirmed") is not False
        or plan.get("name") != fixture["owned_repository_name"]
        or plan.get("owner_native_id") != fixture["owner_native_id"]
        or plan.get("visibility") not in ("private", "internal", "public")
        or plan.get("connection_ref", {}).get("kind") != "github_installation"
        or not plan["connection_ref"].get("id")
    ):
        raise ValueError(
            "Repository confirmation requires a pinned, explicitly owned plan and distinct request keys"
        )
    for reference in ("template_ref", "protections_ref"):
        value = plan.get(reference)
        if (
            not isinstance(value, dict)
            or value.get("tenant_id") != fixture["tenant_id"]
            or value.get("project_id") != fixture["project_id"]
            or not value.get("record_revision")
            or value.get("source_connection_id") != plan["connection_ref"]["id"]
            or not value.get("record_id")
        ):
            raise ValueError(
                "Repository references must resolve inside the owned project"
            )
    observed_before = provider_observer()
    if (
        not isinstance(observed_before, dict)
        or observed_before.get("repositories") != []
    ):
        raise AssertionError("Owned repository name is not unused at the provider")
    owner = fixture["owner"]["access_token"]
    root = (
        base_url.rstrip("/")
        + "/api/v1/products/"
        + quote(fixture["product_id"], safe="")
        + "/projects/"
        + quote(fixture["project_id"], safe="")
    )
    session_url = root + "/onboarding-sessions/" + quote(session_id, safe="")
    session = _read(request, session_url, owner)
    if (
        session.get("id") != session_id
        or session.get("tenant_id") != fixture["tenant_id"]
        or session.get("state") != "open"
    ):
        raise AssertionError("Repository confirmation requires an owned open session")
    revision = session["revision"]

    def send(url, body, key):
        response = request.post(
            url,
            data=body,
            headers={
                "Authorization": "Bearer " + owner,
                "Idempotency-Key": key,
                "If-Profile-Revision": str(revision),
            },
            timeout=30_000,
        )
        if response.status not in (200, 201, 202):
            raise AssertionError(f"Repository operation returned {response.status}")
        return response.json()

    if from_browser:
        preview, preview_key = browser_preview(plan)
    else:
        preview = send(
            session_url + "/repository-plans",
            {"repository_plan": plan},
            keys["preview"],
        )
        preview_key = keys["preview"]
    if not isinstance(preview_key, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{16,128}", preview_key
    ):
        raise AssertionError("Browser preview did not retain its idempotency key")
    if (
        not preview.get("id")
        or preview.get("revision") != revision
        or preview.get("repository_plan") != plan
    ):
        raise AssertionError("Repository preview changed the explicit plan")
    confirmation = {
        "target": "repository_create",
        "action": "create_repository",
        "preview_id": preview["id"],
        "repository_plan": {**plan, "confirmed": True},
    }
    confirm_url = session_url + "/handoffs"
    if from_browser:
        first, confirm_key = browser_confirm(confirmation)
    else:
        confirm_key = keys["confirm"]
        try:
            first = send(confirm_url, confirmation, confirm_key)
        except Exception as error:
            if isinstance(error, (AssertionError, ValueError, TypeError, KeyError)):
                raise
            first = None
    if (
        not isinstance(confirm_key, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", confirm_key)
        or confirm_key in (preview_key, keys["reconcile"])
    ):
        raise AssertionError(
            "Browser confirmation must reuse its distinct recorded request key"
        )
    replay = send(confirm_url, confirmation, confirm_key)
    if first is not None and first.get("id") != replay.get("id"):
        raise AssertionError("Repository confirmation replay started another operation")
    operation_id = replay.get("id")
    if not operation_id or any(
        replay.get(name) != fixture[name]
        for name in ("tenant_id", "product_id", "project_id")
    ):
        raise AssertionError("Repository replay returned another operation or scope")
    operation_url = root + "/operations/" + quote(operation_id, safe="")
    operation = _read(request, operation_url, owner)
    if operation.get("state") == "outcome_unknown":
        recovered = send(operation_url + "/reconcile", {}, keys["reconcile"])
        if recovered.get("id") != operation_id:
            raise AssertionError("Reconciliation changed the original operation")
        operation = _read(request, operation_url, owner)
    receipt = operation.get("repository")
    if (
        operation.get("id") != operation_id
        or operation.get("state") != "succeeded"
        or not isinstance(receipt, dict)
        or receipt.get("coverage") != "complete"
        or receipt.get("protections_applied") is not True
        or receipt.get("applied_plan") != confirmation["repository_plan"]
        or not receipt.get("native_id")
    ):
        raise AssertionError("Reconciliation did not prove the confirmed repository")
    observed_after = provider_observer()
    repositories = (
        observed_after.get("repositories") if isinstance(observed_after, dict) else None
    )
    if (
        not isinstance(repositories, list)
        or len(repositories) != 1
        or repositories[0].get("native_id") != receipt["native_id"]
        or repositories[0].get("owner_native_id") != plan["owner_native_id"]
        or repositories[0].get("name") != plan["name"]
        or repositories[0].get("visibility") != plan["visibility"]
        or repositories[0].get("protections") != fixture["expected_protections"]
        or not observed_after.get("observation_ref")
        or observed_after["observation_ref"] == operation_id
    ):
        raise AssertionError(
            "Independent provider did not confirm exactly one configured repository"
        )
    _no_secrets(operation)
    return {
        "operation": operation,
        "provider": observed_after,
        "before": observed_before,
        "session_id": session_id,
        "confirmation_key": confirm_key,
    }


def adopt_and_cleanup(
    request,
    base_url,
    fixture,
    existing_observer,
    owned_observer,
    delete_owned,
    history_reader,
):
    """Adopt an existing source read-only, unlink it, then delete only owned work."""
    manifest = fixture.get("ownership_manifest")
    selection = fixture.get("existing_selection")
    if (
        not isinstance(manifest, dict)
        or manifest.get("status") != "recorded"
        or not manifest.get("manifest_ref")
        or not isinstance(selection, dict)
        or not all(
            fixture.get(name)
            for name in (
                "run_id",
                "tenant_id",
                "actor_id",
                "product_id",
                "project_id",
                "owner",
            )
        )
        or any(
            manifest.get(name) != fixture[name]
            for name in ("run_id", "tenant_id", "product_id", "project_id")
        )
        or not manifest.get("native_id")
        or not manifest.get("name")
        or not manifest.get("creation_operation_id")
        or selection.get("kind") != "repository"
        or selection.get("provider") != "github"
        or selection.get("native_id") == manifest["native_id"]
        or not isinstance(fixture.get("adoption_keys"), dict)
        or set(fixture["adoption_keys"]) != {"bind", "unlink"}
        or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value)
            for value in fixture["adoption_keys"].values()
        )
        or len(set(fixture["adoption_keys"].values())) != 2
    ):
        raise ValueError(
            "Cleanup requires a durable run-owned manifest and separate read-only existing source"
        )
    baseline = existing_observer()
    owned_before = owned_observer()
    if (
        not isinstance(baseline, dict)
        or not baseline.get("observation_ref")
        or baseline.get("repository", {}).get("native_id") != selection["native_id"]
        or not isinstance(owned_before, dict)
        or not owned_before.get("observation_ref")
        or owned_before.get("repository", {}).get("native_id") != manifest["native_id"]
        or owned_before["repository"].get("name") != manifest["name"]
    ):
        raise AssertionError(
            "Provider baselines do not match the owned and read-only fixtures"
        )
    owner = fixture["owner"]["access_token"]
    root = (
        base_url.rstrip("/")
        + "/api/v1/products/"
        + quote(fixture["product_id"], safe="")
        + "/projects/"
        + quote(fixture["project_id"], safe="")
    )

    def profile():
        observed = _read(request, root, owner)
        if (
            observed.get("tenant_id") != fixture["tenant_id"]
            or observed.get("product", {}).get("starting_point") != "running_product"
        ):
            raise AssertionError("Adoption requires the owned running Product")
        return observed

    before = profile()
    response = request.post(
        root + "/bindings",
        data=selection,
        timeout=30_000,
        headers={
            "Authorization": "Bearer " + owner,
            "Idempotency-Key": fixture["adoption_keys"]["bind"],
            "If-Profile-Revision": str(before["revision"]),
        },
    )
    if response.status != 201 or not response.json().get("id"):
        raise AssertionError(
            "Existing repository adoption did not bind the owned Project"
        )
    binding_id = response.json()["id"]
    if existing_observer().get("repository") != baseline["repository"]:
        raise AssertionError("Adoption changed an existing provider asset")
    after_binding = profile()
    unlink = request.delete(
        root + "/bindings/" + quote(binding_id, safe=""),
        timeout=30_000,
        headers={
            "Authorization": "Bearer " + owner,
            "Idempotency-Key": fixture["adoption_keys"]["unlink"],
            "If-Profile-Revision": str(after_binding["revision"]),
        },
    )
    if unlink.status != 200 or unlink.json().get("status") != "unlinked":
        raise AssertionError("Adopted repository binding was not unlinked")
    if (
        existing_observer().get("repository") != baseline["repository"]
        or owned_observer().get("repository") != owned_before["repository"]
    ):
        raise AssertionError("Unlink modified existing or owned provider resources")
    receipt = delete_owned(manifest)
    owned_after = owned_observer()
    if (
        not isinstance(receipt, dict)
        or receipt.get("run_id") != manifest["run_id"]
        or receipt.get("native_id") != manifest["native_id"]
        or receipt.get("status") != "verified"
        or not receipt.get("receipt_ref")
        or not isinstance(owned_after, dict)
        or not owned_after.get("observation_ref")
        or owned_after.get("repository") is not None
        or existing_observer().get("repository") != baseline["repository"]
    ):
        raise AssertionError(
            "Only the run-owned repository may be deleted; provider absence is required"
        )
    history = history_reader()
    if (
        not isinstance(history, list)
        or len(history) < 2
        or any(not isinstance(item, dict) for item in history)
        or not any(
            item.get("operation_id") == manifest.get("creation_operation_id")
            and item.get("native_id") == manifest["native_id"]
            for item in history
        )
        or not any(
            item.get("receipt_ref") == receipt["receipt_ref"]
            and item.get("native_id") == manifest["native_id"]
            for item in history
        )
    ):
        raise AssertionError(
            "Creation and cleanup receipt history must remain retrievable"
        )
    return {
        "binding_id": binding_id,
        "owned_before": owned_before,
        "owned_after": owned_after,
        "existing_before": baseline,
        "existing_after": existing_observer(),
        "cleanup": receipt,
        "history": history,
    }
