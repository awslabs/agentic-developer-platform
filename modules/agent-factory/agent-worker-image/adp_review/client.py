"""GitHub review submission with an honest self-review fallback (issue #5350)."""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("adp_review")

GITHUB_API = "https://api.github.com"

# The two events that carry a verdict, and the one that does not. GitHub refuses
# the first two from the PR's own author; COMMENT is always allowed, which is
# exactly why a COMMENT must never be reported as an approval.
VERDICT_EVENTS = ("APPROVE", "REQUEST_CHANGES")
COMMENT_EVENT = "COMMENT"
VALID_EVENTS = (*VERDICT_EVENTS, COMMENT_EVENT)

# The review STATE GitHub must report back for a verdict to actually be recorded.
# A 2xx alone does not establish it: on PR #5346 a reviewer run reported success
# while `GET /pulls/5346/reviews` returned zero reviews and `reviewDecision` stayed
# empty. So the answer is read from the response, never inferred from the request —
# inferring it is how a comment-only outcome gets reported as an approval.
VERDICT_STATES = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}

# Explanation used when GitHub accepted the review but recorded a state that carries
# no verdict. Deliberately NOT the 422 wording: claiming a refusal that did not
# happen would be a checkable falsehood in the message whose only job is honesty.
_DOWNGRADED_REASON = (
    "GitHub accepted the review but recorded it with state `{state}` rather than the "
    "state a `{event}` verdict requires, so the verdict was not recorded"
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PENDING_APPROVAL = 3

# GitHub's own wording for the refusal, lowercased for matching. Matched on the
# message rather than the status alone because 422 is also returned for ordinary
# validation faults (an unknown event name, a bad commit id), and those must NOT be
# reported to a human as "approval pending" — they are bugs in the call.
_SELF_REVIEW_MARKERS = (
    "can not approve your own pull request",
    "can not request changes on your own pull request",
)


class ReviewError(Exception):
    """The review could not be published at all."""


def _self_review_refusal(status: int, body: str) -> bool:
    """True if GitHub refused this review because author == reviewer."""
    if status != 422:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in _SELF_REVIEW_MARKERS)


def _github_token() -> str:
    """Return the review token.

    Prefers ADP_REVIEW_TOKEN (minted for the reviewer identity by ``mint``) and
    falls back to the run's ordinary token. The fallback is what makes the
    pending-approval path reachable rather than a hard failure.
    """
    for var in ("ADP_REVIEW_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    token_file = os.environ.get("ADP_TOKEN_FILE", "/tmp/.adp-gh-token")
    try:
        with open(token_file, encoding="ascii") as handle:
            token = handle.read().strip()
        if token:
            return token
    except OSError:
        pass
    raise ReviewError("No GitHub token available (set ADP_REVIEW_TOKEN or GH_TOKEN, or provide ADP_TOKEN_FILE)")


def _api(method: str, path: str, payload: dict | None, token: str) -> tuple[int, str]:
    """Call the GitHub API. Returns ``(status, body)``; never raises on 4xx/5xx.

    HTTP errors are returned rather than raised precisely because the 422 is the
    signal this whole module exists to read, not an exception to escape through.
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{GITHUB_API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "adp-review",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001 - the STATUS is what callers branch on; an
            # unreadable body must never turn a 422 into an exception, or the
            # self-review refusal becomes invisible again.
            return exc.code, ""
    except URLError as exc:
        raise ReviewError(f"Cannot reach GitHub: {exc.reason}") from None


def mint_review_token(*, repo: str) -> tuple[str | None, str]:
    """Mint a token for the distinct REVIEWER identity via the gateway.

    Returns ``(token_or_None, granted_identity)``. ``granted_identity`` is what the
    gateway actually used — "review" only if a distinct reviewer App is configured,
    "default" when it fell back. A caller must branch on the identity, not on
    whether a token came back: the fallback token works fine for commenting and
    cannot carry a verdict.

    Returns ``(None, "default")`` when the gateway is unreachable or the mint is
    refused. That is deliberately not fatal — the run still has its ordinary token
    and can still publish the verdict with a pending-approval notice, which is
    strictly better than losing the review.
    """
    sys.path.insert(0, "/app")
    try:
        from lib.gateway_credential_client import GatewayCredentialClient, GatewayCredentialError
    except ImportError:
        logger.info("Gateway credential client unavailable; using the run's existing identity")
        return None, "default"

    installation_id = os.environ.get("GH_APP_INSTALLATION_ID", "").strip()
    if not installation_id:
        logger.info("GH_APP_INSTALLATION_ID is not set; using the run's existing identity")
        return None, "default"

    try:
        owner, name = repo.split("/", 1)
    except ValueError:
        raise ReviewError(f"repo must be OWNER/NAME, got {repo!r}") from None

    client = GatewayCredentialClient()
    if not client.is_configured:
        logger.info("Gateway not reachable from this pod; using the run's existing identity")
        return None, "default"

    try:
        result = client.github_installation_token(
            installation_id=int(installation_id),
            repo_owner=owner,
            repo_name=name,
            identity="review",
            purpose="formal PR review (adp-review)",
        )
    except (GatewayCredentialError, ValueError) as exc:
        logger.info("Reviewer identity mint refused (%s); using the run's existing identity", type(exc).__name__)
        return None, "default"

    # A gateway that predates #5350 returns no `identity` field. Treat that as
    # "default": assuming "review" from silence is exactly the optimism that let
    # this defect hide.
    return result.get("token"), result.get("identity") or "default"


def submit_review(
    *,
    repo: str,
    pr_number: int,
    event: str,
    body: str,
    commit_id: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Submit a formal review, falling back honestly when GitHub refuses it.

    Returns a result dict with ``outcome`` one of:
      ``submitted``        — the formal review carries a verdict
      ``pending_approval`` — GitHub refused the self-review; the verdict was
                             published as a comment and a human approval is pending

    Raises:
        ReviewError: if the verdict could not be published at all. A refused
            verdict that still reached the PR is NOT an error — losing the review
            entirely is worse than recording it without a verdict.
    """
    if event not in VALID_EVENTS:
        raise ReviewError(f"event must be one of {', '.join(VALID_EVENTS)}, got {event!r}")
    if not body.strip():
        raise ReviewError("review body is empty; a verdict with no reasoning is not a review")

    token = token or _github_token()
    payload: dict[str, Any] = {"event": event, "body": body}
    if commit_id:
        payload["commit_id"] = commit_id

    status, raw = _api("POST", f"/repos/{repo}/pulls/{pr_number}/reviews", payload, token)

    if 200 <= status < 300:
        try:
            review = json.loads(raw)
        except json.JSONDecodeError:
            # An unparseable body cannot establish that the verdict landed. Guess
            # optimistically here and a verdict-less review reports success, which is
            # the #5346 failure exactly; so an unverifiable state counts as NOT
            # recorded and takes the pending-approval path below.
            review = {}
        state = review.get("state")
        expected_state = VERDICT_STATES.get(event)

        if expected_state is not None and state != expected_state:
            # GitHub ACCEPTED the call but did not record the verdict: a request for
            # APPROVE came back COMMENTED (or with no readable state). This is the
            # silent no-op that made a reviewer run go green while the PR had no
            # review on it at all. Report it as the pending approval it is.
            logger.error(
                "GitHub accepted the %s on %s#%s but recorded state=%r, not %r — the verdict "
                "was NOT recorded. Naming the pending human approval instead of reporting "
                "success.",
                event,
                repo,
                pr_number,
                state,
                expected_state,
            )
            # The analysis already reached the PR (GitHub accepted it), but with no
            # verdict and no notice saying so. Post the notice as a follow-up comment
            # so a human reading the PR learns an approval is still required —
            # without it this path is silent on the PR itself, which is the defect.
            notice_url = None
            notice_status, notice_raw = _api(
                "POST",
                f"/repos/{repo}/issues/{pr_number}/comments",
                {"body": pending_approval_notice(event, reason=_DOWNGRADED_REASON.format(event=event, state=state)).lstrip()},
                token,
            )
            if 200 <= notice_status < 300:
                try:
                    notice_url = json.loads(notice_raw).get("html_url")
                except json.JSONDecodeError:
                    notice_url = None
            else:
                logger.error(
                    "Could not publish the pending-approval notice on %s#%s (HTTP %s); the "
                    "verdict is recorded nowhere as pending.",
                    repo,
                    pr_number,
                    notice_status,
                )

            return {
                "outcome": "pending_approval",
                "event": event,
                "published_as": "comment_review",
                "review_id": review.get("id"),
                "state": state,
                "url": review.get("html_url"),
                "notice_url": notice_url,
                "verdict_recorded": False,
                "pending_human_approval": True,
            }

        return {
            "outcome": "submitted",
            "event": event,
            "review_id": review.get("id"),
            "state": state,
            "url": review.get("html_url"),
            # True only when GitHub reported the state that carries the verdict.
            "verdict_recorded": expected_state is not None,
        }

    if _self_review_refusal(status, raw):
        # The defect, finally visible. Log it loudly: for the whole life of the
        # engine this refusal was silent, which is why reviewers "went quiet"
        # instead of reporting that they could not deliver a verdict.
        logger.error(
            "GitHub REFUSED the formal %s on %s#%s: author and reviewer are the same "
            "GitHub App (HTTP 422 self-review). Publishing the verdict as a comment and "
            "naming the pending human approval. Configure a distinct reviewer App to "
            "make a formal verdict possible.",
            event,
            repo,
            pr_number,
        )
        return _publish_pending_approval(repo=repo, pr_number=pr_number, event=event, body=body, token=token)

    raise ReviewError(f"GitHub refused the review (HTTP {status}): {raw[:400]}")


def pending_approval_notice(event: str, *, reason: str | None = None) -> str:
    """The notice appended when a verdict could not be formally recorded.

    Explicit about three things a reader needs and could not previously get: that
    this is NOT a formal review, why, and who has to act.

    ``reason`` overrides the explanation for a non-422 cause — notably GitHub
    accepting the call but recording a non-verdict state. Stating the 422 reason
    there would be a confident, checkable falsehood in the one message whose job
    is to be honest about what happened.
    """
    intent = "approval" if event == "APPROVE" else "change request"
    if reason is None:
        reason = (
            "GitHub refused to record it: this pull request's "
            "author and this reviewer are the same GitHub App, and GitHub does not allow "
            f"`{event}` on your own pull request (HTTP 422)"
        )
    return (
        "\n\n---\n\n"
        f"**⚠️ This is not a formal GitHub review — a human {intent} is still pending.**\n\n"
        f"The verdict above is `{event}`, but {reason}. The verdict is published here as a "
        "comment so the analysis is not lost, but a comment does **not** set `reviewDecision` — "
        "so as far as any merge gate is concerned, this pull request has no verdict.\n\n"
        f"**A human reviewer with write access must submit the formal {intent}** before this "
        "pull request can satisfy a review gate.\n\n"
        "_This is a platform limitation, not a judgement about the code. It is resolved by "
        "configuring a distinct reviewer GitHub App identity (issue #5350)._"
    )


def _publish_pending_approval(*, repo: str, pr_number: int, event: str, body: str, token: str) -> dict[str, Any]:
    """Publish a refused verdict as a comment that names the pending approval.

    Tries a COMMENT review first (it lands in the PR's review timeline, where a
    reviewer's verdict belongs) and falls back to an issue comment if even that is
    refused. If BOTH fail the verdict is lost, which IS an error.
    """
    annotated = body + pending_approval_notice(event)

    status, raw = _api(
        "POST",
        f"/repos/{repo}/pulls/{pr_number}/reviews",
        {"event": COMMENT_EVENT, "body": annotated},
        token,
    )
    if 200 <= status < 300:
        try:
            review = json.loads(raw)
        except json.JSONDecodeError:
            review = {}
        return {
            "outcome": "pending_approval",
            "event": event,
            "published_as": "comment_review",
            "review_id": review.get("id"),
            "state": review.get("state"),
            "url": review.get("html_url"),
            # The single most important field in this module: the verdict exists as
            # prose but was NOT recorded, so no caller may treat it as approval.
            "verdict_recorded": False,
            "pending_human_approval": True,
        }

    status, raw = _api("POST", f"/repos/{repo}/issues/{pr_number}/comments", {"body": annotated}, token)
    if 200 <= status < 300:
        try:
            comment = json.loads(raw)
        except json.JSONDecodeError:
            comment = {}
        return {
            "outcome": "pending_approval",
            "event": event,
            "published_as": "issue_comment",
            "url": comment.get("html_url"),
            "verdict_recorded": False,
            "pending_human_approval": True,
        }

    raise ReviewError(f"GitHub refused the formal review AND the fallback comment (HTTP {status}): {raw[:400]}")
