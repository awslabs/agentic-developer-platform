"""Exercise maintained create/removal controls with original, verified request receipts.

Only nonsecret request/approval identities are restored. Service responses remain the
source of approval, execution and readiness facts; no response is synthesized.
"""

import json
from urllib.parse import quote

from .demo1_evidence import EvidenceError

PREFIX = "/api/superplane/v1"


def require(condition):
    if not condition:
        raise EvidenceError(
            "browser controls: saved identity or rendered request differs"
        )


def fingerprint(payload):
    value = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    result = 0x811C9DC5
    # Match JavaScript charCodeAt over UTF-16 code units.
    encoded = value.encode("utf-16-le")
    for offset in range(0, len(encoded), 2):
        result = (
            (result ^ int.from_bytes(encoded[offset : offset + 2], "little"))
            * 0x01000193
        ) & 0xFFFFFFFF
    return f"{result:08x}"


def restore_receipt(
    transport,
    *,
    request_id,
    workspace_id,
    approval_id,
    intent,
    payload,
    principal=False,
    recovery=False,
    operation_id=None,
):
    selected = transport.selected
    require(selected is not None and request_id and workspace_id and approval_id)
    scope = {"deploymentId": transport.origin, "orgId": selected.org_id}
    suffix = ""
    if principal:
        scope["principalId"] = selected.requester_id
        suffix = "principal:" + quote(selected.requester_id, safe="~()*!.'-") + "."
    key = f"adp.superplane.onboarding.receipt.{transport.origin}.{selected.org_id}.{suffix}{intent}"
    receipt = {
        "idempotencyKey": request_id,
        "operationId": operation_id,
        "fingerprint": fingerprint(payload),
        "scope": scope,
        "createdAt": selected.authorized_at.isoformat(),
        "state": "unknown",
        "workspaceId": workspace_id,
        "submissionStage": "submitted" if recovery else "approval",
        "approvalId": approval_id,
    }
    outcome = transport.page.evaluate(
        """({key, receipt, recovery}) => {
          const raw = localStorage.getItem(key);
          if (raw !== null) {
            let saved; try { saved = JSON.parse(raw); } catch { return false; }
            const fields = ['idempotencyKey', 'fingerprint', 'approvalId', 'workspaceId'];
            if (fields.some(k => saved[k] !== receipt[k]) ||
                JSON.stringify(saved.scope) !== JSON.stringify(receipt.scope)) return false;
            if (recovery) {
              if (saved.submissionStage !== 'submitted' ||
                  (receipt.operationId && saved.operationId && receipt.operationId !== saved.operationId)) return false;
              return true;
            }
            if (saved.submissionStage === 'submitted' || saved.operationId) return false;
          }
          localStorage.setItem(key, JSON.stringify(receipt));
          return localStorage.getItem(key) === JSON.stringify(receipt);
        }""",
        {"key": key, "receipt": receipt, "recovery": recovery},
    )
    require(outcome is True)
    return key, receipt


def _response(transport, response):
    require(response.headers.get("x-superplane-release") == transport.release_id)
    require(response.status in (200, 201))
    value = response.json()
    require(isinstance(value, dict))
    return response.status, value


def _click_response(transport, button, path, expected, *, before_send=None):
    page, url = transport.page, transport.origin + path
    seen, failure = [], []

    def guard(route):
        request = route.request
        if request.method != "POST":
            route.fallback()
            return
        try:
            require(
                request.url == url and request.post_data_json == expected and not seen
            )
            # This durable checkpoint precedes transmission, including a lost reply.
            if before_send is not None:
                before_send()
            seen.append(True)
        except Exception as error:
            failure.append(error)
            route.abort()
            return
        route.fallback()

    page.route(url, guard)
    try:
        with page.expect_response(
            lambda response: response.url == url and response.request.method == "POST",
            timeout=min(30_000, transport.remaining_ms()),
        ) as pending:
            button.click(timeout=min(30_000, transport.remaining_ms()))
        require(not failure and len(seen) == 1)
        result = _response(transport, pending.value)
        return result
    except Exception:
        if failure:
            raise EvidenceError(
                "browser controls: outbound request refused before transmission"
            ) from None
        raise
    finally:
        page.unroute(url, guard)


def create_workspace(transport, body, before_send):
    require(transport.selected is not None)
    selected = transport.selected
    status, identity = transport.request("GET", "/api/auth/me")
    require(
        status == 200
        and identity.get("user_id") == selected.requester_id
        and identity.get("org_id") == selected.org_id
    )
    # This is the maintained UI buildCreatePayload shape. It is checked again on
    # the actual outgoing request, including all nullable defaults and the cap.
    payload = {
        "mode": body["mode"],
        "cluster_reference": None,
        "name": body["name"],
        "isolation_mode": body["isolation_mode"],
        "account": body["account"],
        "region": body["region"],
        "budget_max_daily_usd": float(body["budget_max_daily_usd"]),
        "budget_max_gpus": None,
    }
    # JSON.stringify serializes integral numbers without a .0 suffix.
    if payload["budget_max_daily_usd"].is_integer():
        payload["budget_max_daily_usd"] = int(payload["budget_max_daily_usd"])
    restore_receipt(
        transport,
        request_id=body["operation_id"],
        workspace_id=transport.creation_workspace_id,
        approval_id=body["approval_id"],
        intent="create-workspace",
        payload=payload,
    )
    page = transport.page
    page.reload(timeout=min(30_000, transport.remaining_ms()))
    recover = page.get_by_role("button", name="Review original request", exact=True)
    if recover.is_visible():
        recover.click()
    else:
        page.get_by_role("button", name="Create a workspace", exact=True).click()
    form = page.get_by_role("region", name="Create a workspace", exact=True)
    for label, value in (
        ("Workspace name", body["name"]),
        ("Target account", body["account"]),
        ("Region", body["region"]),
        ("Daily spend cap (USD)", str(body["budget_max_daily_usd"])),
    ):
        form.get_by_label(label, exact=True).fill(value)
    form.get_by_label("Isolation mode", exact=True).select_option(
        body["isolation_mode"]
    )
    preview = {**payload, "operation_id": body["operation_id"]}
    _, reviewed = _click_response(
        transport,
        form.get_by_role("button", name="Review plan", exact=True),
        PREFIX + "/workspaces/preview",
        preview,
    )
    require(
        reviewed.get("revision") == body["plan_revision"]
        and reviewed.get("workspace_id") == transport.creation_workspace_id
    )
    outgoing = {
        **preview,
        "plan_revision": body["plan_revision"],
        "approval_id": body["approval_id"],
    }
    return _click_response(
        transport,
        form.get_by_role("button", name="Create this workspace", exact=True),
        PREFIX + "/workspaces",
        outgoing,
        before_send=before_send,
    )


def remove_workspace(transport, saved, before_send):
    require(transport.selected is not None and not saved.submitted)
    restore_receipt(
        transport,
        request_id=saved.request_id,
        workspace_id=saved.workspace_id,
        approval_id=saved.approval_id,
        intent="retire-workspace:" + saved.workspace_id,
        payload={"workspaceId": saved.workspace_id},
        principal=True,
    )
    page = transport.page
    page.reload(timeout=min(30_000, transport.remaining_ms()))
    from .demo1_c1 import inspect_reentry

    require(
        inspect_reentry(page, transport.selected.workspace_name)["reason"]
        == "read-only re-entry visible; sign-in, authority and Ready not independently proved"
    )
    region = page.get_by_role("region", name="Workspace retirement", exact=True)
    _, review = _click_response(
        transport,
        region.get_by_role("button", name="Review removal", exact=True),
        PREFIX + f"/workspaces/{saved.workspace_id}/retirement/preview",
        {"operation_id": saved.request_id},
    )
    require(
        review.get("revision") == saved.revision
        and review.get("source_operation_id") == saved.source_operation_id
    )
    body = {
        "operation_id": saved.request_id,
        "plan_revision": saved.revision,
        "approval_id": saved.approval_id,
    }
    return _click_response(
        transport,
        region.get_by_role("button", name="Remove workspace", exact=True),
        PREFIX + f"/workspaces/{saved.workspace_id}/retirement",
        body,
        before_send=before_send,
    )


def recover_removal(transport, saved):
    """Read the original removal through the maintained recovery control."""
    from .demo1_c1 import inspect_reentry

    require(saved.submitted)
    restore_receipt(
        transport,
        request_id=saved.request_id,
        workspace_id=saved.workspace_id,
        approval_id=saved.approval_id,
        intent="retire-workspace:" + saved.workspace_id,
        payload={"workspaceId": saved.workspace_id},
        principal=True,
        recovery=True,
        operation_id=saved.operation_id,
    )
    transport.page.reload(timeout=min(30_000, transport.remaining_ms()))
    require(
        inspect_reentry(transport.page, transport.selected.workspace_name)["reason"]
        == "read-only re-entry visible; sign-in, authority and Ready not independently proved"
    )
    page = transport.page
    url = transport.origin + PREFIX + f"/operations/by-idempotency/{saved.request_id}"
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "GET",
        timeout=min(30_000, transport.remaining_ms()),
    ) as response:
        page.get_by_role("button", name="Recover removal request", exact=True).click()
    _, value = _response(transport, response.value)
    outcome = page.get_by_label("Retirement outcome", exact=True)
    outcome.get_by_text(
        "Operation state: "
        + {"pending": "accepted"}.get(
            value.get("state"), value.get("state", "unknown")
        ),
        exact=True,
    ).wait_for(state="visible", timeout=min(30_000, transport.remaining_ms()))
    return value
