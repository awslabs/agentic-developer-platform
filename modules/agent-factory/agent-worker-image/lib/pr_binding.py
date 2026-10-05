"""Register the pull request this run delivered with the orchestration engine (#5301).

The worker half of the story-to-PR binding. When the engine dispatches a code
story it now marks the envelope ``pr_binding_required``; this module reports back
which pull request actually carried the work, so completion can be verified from
that PR instead of from whether the issue happened to be closed by a closing
keyword.

--------------------------------------------------------------------------------
Why this exists at all
--------------------------------------------------------------------------------

Before it, the engine knew a story had been dispatched and knew the worker had
exited, but never recorded *which pull request implemented it*. Completion was
inferred from the GitHub issue's closure timeline, which only exists when the PR
body used a closing keyword. A body saying ``Issue #5049`` creates no closing
event, so the story waited in ``awaiting_merge`` on evidence that would never
arrive while its PR sat merged. Sending the association is what closes that.

--------------------------------------------------------------------------------
Acknowledgement without rerunning development
--------------------------------------------------------------------------------

Shared-role dispatch includes a scoped report credential. The gateway persists
an exact PR candidate before provider I/O and returns a checked binding receipt.
Transport retries and queue redelivery retry this report, preserving pushed work.
Advisory comments remain fail-soft; the entrypoint checks ``handoff_pending`` and
keeps the queue message when its required acknowledgement is absent.

--------------------------------------------------------------------------------
What is sent, and what is deliberately not
--------------------------------------------------------------------------------

Only the pull request's own identity: the provider's immutable repository id and
PR node id, the display ``owner/name`` and number, and the current head SHA.

Nothing about the *story*. No node id, flow id, tenant or run id — the gateway
derives all of those from the run credential this request authenticates with, so
the worker cannot bind its PR to a story it was not dispatched for even if it
tried. That is why the immutable ids are fetched from the provider here rather
than assembled from strings: a binding keyed on a name is one a repository rename
can re-point.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time

from lib import run_report

from lib.status_gateway_client import (
    StatusGatewayError,
    authority_enabled,
    post_self,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BINDING_REQUIRED_ENV",
    "binding_note",
    "binding_required",
    "register_pull_request",
]

# Set during bootstrap from the envelope's `pr_binding_required` flag. Absent for a
# webhook-triggered run and for a legacy engine dispatch, both of which register
# nothing: this module is inert unless the engine asked for a binding.
BINDING_REQUIRED_ENV = "ADP_PR_BINDING_REQUIRED"

_TRUE_SPELLINGS = frozenset({"1", "true", "yes", "on"})
_GH_TIMEOUT_SECONDS = 30
_handoff_pending = False


def handoff_pending() -> bool:
    return binding_required() and _handoff_pending


def _verified_receipt(snapshot: dict, candidate: dict) -> dict:
    receipt = snapshot.get("binding_receipt") or {}
    if receipt.get("bound") is not True or any(receipt.get(key) != candidate.get(key) for key in (
        "repo", "pr_number", "provider_repository_id", "provider_pr_node_id", "head_sha"
    )):
        raise run_report.RunReportError(snapshot.get("block_code") or "pr_binding_unacknowledged",
                                       retryable=snapshot.get("retryable") is not False)
    return receipt


def resume_handoff() -> bool:
    """Return true after replaying a persisted delivery; never launch development."""
    snapshot = run_report.request()
    # request() authenticates the reporting capability and checks run/attempt
    # identity. Only this explicit permanent server decision retires a queue
    # delivery; HTTP errors, expiry and unknown ownership do not prove retirement.
    if (snapshot.get("block_code") == "execution_assignment_superseded"
            and snapshot.get("retryable") is False):
        raise run_report.SupersededDelivery()
    if snapshot.get("terminal_receipt"):
        return True
    if (run_report._assignment or {}).get("reviewer_owned_delivery"):
        spool = run_report.read_spool()
        if spool and spool["phase"] == "review":
            if spool.get("ownership_nonce") != (snapshot.get("worker_receipt") or {}).get("ownership_nonce"):
                raise run_report.RunReportError("delivery_recovery_required", retryable=False)
            from lib import status_gateway_client

            status_gateway_client.upload_review_result(spool["review_content"].encode())
            document = json.loads(spool["review_content"])
            if document.get("verdict") == "approve":
                view = subprocess.run(["gh", "pr", "view", str(document["subject"]["pr_number"]),
                    "-R", document["repository"]["repo"], "--json", "mergedAt,headRefOid"],
                    check=True, capture_output=True, text=True, timeout=_GH_TIMEOUT_SECONDS)
                pr = json.loads(view.stdout)
                if not pr.get("mergedAt") or pr.get("headRefOid") != document["subject"]["reviewed_head_sha"]:
                    # Evidence alone is not delivery and may belong to a worker
                    # still merging. Never launch another model from redelivery.
                    raise run_report.RunReportError("delivery_recovery_required", retryable=False)
            run_report.terminal("complete")
            return True
        if (snapshot.get("review_receipt") or {}).get("recorded") is True:
            raise run_report.RunReportError("delivery_recovery_required", retryable=False)
    if (snapshot.get("review_receipt") or {}).get("recorded") is True:
        run_report.terminal("complete")
        return True
    candidate = snapshot.get("candidate_pr")
    if candidate is None:
        spool = run_report.read_spool()
        if spool is None:
            return False
        if spool["phase"] == "failed":
            owner = (snapshot.get("worker_receipt") or {}).get("ownership_nonce")
            if not owner or owner != spool["ownership_nonce"]:
                raise run_report.RunReportError("delivery_recovery_required", retryable=False)
            run_report.terminal("failed", **({"failure": spool["failure"]} if spool.get("failure") else {}))
            return True
        if run_report.can_retry_start(spool, snapshot):
            return False
        if spool["phase"] != "candidate" or not isinstance(spool.get("candidate_pr"), dict):
            raise run_report.RunReportError("delivery_recovery_required", retryable=False)
        candidate = spool["candidate_pr"]
        snapshot = run_report.request("/pull-request", candidate)
    if not snapshot.get("binding_receipt"):
        snapshot = run_report.request("/pull-request/retry", {})
    _verified_receipt(snapshot, candidate)
    run_report.terminal("complete")
    return True


def binding_required() -> bool:
    """Whether the engine asked this run to register its pull request.

    Defaults to **false**: a run whose envelope carried no marker (a webhook
    trigger, or an engine dispatch from before this contract) registers nothing and
    behaves exactly as it did before this module existed.
    """
    return os.environ.get(BINDING_REQUIRED_ENV, "").strip().lower() in _TRUE_SPELLINGS


def _pr_identity(repo: str, pr_number: int) -> dict[str, object]:
    """Fetch the pull request's immutable provider identity and current head.

    Uses the ``gh`` CLI already authenticated for this run. ``id`` is the PR's
    GraphQL node id; the numeric repository id comes from :func:`_repository_id`.
    The gateway refuses a registration without both (``INCOMPLETE_IDENTITY``)
    rather than binding on a mutable name.
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "-R",
            repo,
            "--json",
            "id,number,headRefOid",
        ],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT_SECONDS,
        check=True,
    )
    view = json.loads(result.stdout or "{}")
    head_sha = (view.get("headRefOid") or "").strip()
    node_id = (view.get("id") or "").strip()
    repository_id = _repository_id(repo)
    if not head_sha or not node_id or not repository_id:
        raise StatusGatewayError("pull request identity is incomplete")
    return {
        "provider_repository_id": repository_id,
        "provider_pr_node_id": node_id,
        "repo": repo,
        "pr_number": int(pr_number),
        "head_sha": head_sha,
    }


def _repository_id(repo: str) -> int:
    """The repository's immutable numeric id, as the binding keys on it.

    Asked for explicitly rather than taken from ``gh pr view``'s
    ``headRepository.id``, which is a GraphQL node id and not the numeric id the
    binding stores.
    """
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}", "--jq", ".id"],
        capture_output=True,
        text=True,
        timeout=_GH_TIMEOUT_SECONDS,
        check=True,
    )
    raw = (result.stdout or "").strip()
    return int(raw) if raw.isdigit() else 0


def register_pull_request(*, repo: str, pr_number: int, reviewer_artifact: bool = False) -> dict:
    """Bind this run's pull request to the story it was dispatched for.

    Raises:
        StatusGatewayError: the registration was refused or could not be sent.
            Callers should use :func:`binding_note`, which converts this to a note.
    """
    payload = _pr_identity(repo, pr_number)
    if reviewer_artifact:
        # A caller may only ever *downgrade* itself; the gateway ignores an attempt
        # to claim a stronger role than its dispatch already implies.
        payload["reviewer_artifact"] = True
    if run_report.enabled():
        # Independent existing artifact storage survives a gateway outage. This
        # is only an assertion to replay, never authority or binding evidence.
        try:
            run_report.spool_candidate(payload)
        except run_report.RunReportError:
            # Gateway staging may still make it durable. If both fail, the
            # executing marker forbids rerunning development on redelivery.
            logger.warning("PR candidate spool unavailable; requiring gateway acknowledgement")
        error = None
        for attempt in range(3):
            try:
                snapshot = run_report.request("/pull-request", payload)
                receipt = _verified_receipt(snapshot, payload)
                # Check durable readback, including the exact run, attempt and PR.
                _verified_receipt(run_report.request(), payload)
                return receipt
            except run_report.RunReportError as exc:
                error = exc
                if not exc.retryable:
                    break
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise error
    result = post_self("/pull-request", payload)
    if (result.get("bound") is not True or result.get("pr_number") != payload["pr_number"]
            or result.get("head_sha") != payload["head_sha"]):
        raise StatusGatewayError("pull-request binding was not acknowledged for the delivered revision")
    return result


def binding_note(*, repo: str, pr_number: str | int | None, reviewer_artifact: bool = False) -> str:
    """Register the PR and return a comment section. Never raises.

    Returns "" when registration does not apply — not an engine story dispatch, or
    no PR to bind — so the caller appends nothing and the closing comment is
    unchanged for every pre-existing path.
    """
    global _handoff_pending
    _handoff_pending = binding_required()
    if not binding_required():
        return ""
    if not pr_number:
        if run_report.enabled():
            run_report.report_block("pr_candidate_missing")
        return "> **Story pull-request handoff blocked:** pr_candidate_missing. No completion acknowledgement was recorded."
    try:
        number = int(pr_number)
    except (TypeError, ValueError):
        return ""
    if not authority_enabled() and not run_report.enabled():
        # The gateway path is how a binding is authenticated; without it there is no
        # way to register one, and the engine will hold the story with a stated
        # reason. Say so rather than failing quietly.
        return (
            "> **Story pull-request binding skipped.** Agent authority is not enabled for this "
            "deployment, so this run could not register its pull request with the engine. The "
            "story will wait until the association is recorded."
        )
    try:
        result = register_pull_request(repo=repo, pr_number=number, reviewer_artifact=reviewer_artifact)
    except (StatusGatewayError, run_report.RunReportError) as exc:
        # Visible, not silent: an unregistered PR means the story holds.
        logger.warning("pull-request binding failed for %s#%s: %s", repo, number, exc)
        return (
            f"> **Story pull-request binding failed** for PR #{number}: {exc}. The engine cannot "
            "verify this story's merge until the association is recorded, so it will hold with that "
            "reason rather than completing."
        )
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.warning("pull-request binding could not read PR identity for %s#%s: %s", repo, number, exc)
        return (
            f"> **Story pull-request binding failed** for PR #{number}: the pull request's provider "
            "identity could not be read. The engine will hold this story until the association is recorded."
        )
    _handoff_pending = False
    role = result.get("role", "implementation")
    if result.get("created"):
        return f"> Registered PR #{number} as this story's {role} pull request."
    # Not created: an idempotent retry, or a head repair on the same PR. Reported as
    # such rather than as a fresh registration.
    return f"> PR #{number} is already registered as this story's {role} pull request."
