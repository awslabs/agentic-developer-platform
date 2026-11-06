"""Issue notice for a reply dropped while an active run still owned the work.

Issue #9541: a human's gate reply that arrives while the previous run on the
same issue is still finishing is refused by work admission. Before this module
the webhook Lambda answered GitHub with a 500 and nothing reached the issue, so
the user believed the agent was working. When the bounded admission wait in
``sqs_publisher`` is exhausted the handler calls :func:`post_work_owned_notice`
so the issue itself says what happened and how to repeat the reply.

The notice is posted with a short-lived installation token minted for the one
repository and the ``issues: write`` permission only. Every failure is logged
and swallowed: the notice is best effort and must never turn a dropped reply
into a second failure mode.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

from common import github_actor_authority as _authority

logger = logging.getLogger(__name__)

NOTICE_MARKER = "<!-- adp-work-owned-notice -->"
NOTICE_BODY = (
    f"{NOTICE_MARKER}\n"
    "Your reply arrived while the previous agent run on this issue was still "
    "finishing, so it was **not** accepted. The run owns this issue until it "
    "posts its completion marker.\n\n"
    "Please repeat your reply once the previous run has posted its final "
    "comment. Nothing from this message was queued."
)

# Seconds the whole notice path may take: token mint plus one comment POST.
NOTICE_DEADLINE_SECONDS = 6.0


def post_work_owned_notice(
    *,
    installation_id: object,
    repo_full_name: object,
    repository_id: object,
    issue_number: object,
) -> bool:
    """Post the dropped-reply notice; return True only when GitHub accepted it."""
    installation = _authority._account_id(installation_id)
    repository = _authority._account_id(repository_id)
    issue = _authority._account_id(issue_number)
    if (
        installation is None
        or repository is None
        or issue is None
        or not isinstance(repo_full_name, str)
        or repo_full_name.count("/") != 1
    ):
        logger.warning("Work-owned notice skipped: incomplete issue reference")
        return False
    owner, name = repo_full_name.split("/")
    if not owner or not name:
        logger.warning("Work-owned notice skipped: incomplete issue reference")
        return False
    deadline = time.monotonic() + NOTICE_DEADLINE_SECONDS
    try:
        import jwt

        app_id, key = _authority._app_credentials(deadline)
        now = int(time.time())
        app_jwt = jwt.encode(
            {"iat": now - 60, "exp": now + 600, "iss": app_id}, key, algorithm="RS256"
        )
        status, token_body = _authority._request(
            f"/app/installations/{installation}/access_tokens",
            app_jwt,
            deadline,
            method="POST",
            body={
                "repository_ids": [repository],
                "permissions": {"issues": "write"},
            },
        )
        if (
            status != 201
            or not isinstance(token_body, dict)
            or not token_body.get("token")
        ):
            logger.warning("Work-owned notice skipped: installation token unavailable")
            return False
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/issues/{issue}/comments"
        )
        status, _ = _authority._request(
            path,
            token_body["token"],
            deadline,
            method="POST",
            body={"body": NOTICE_BODY},
        )
    except Exception:
        logger.warning(
            "Work-owned notice failed repo=%s issue=%s", repo_full_name, issue
        )
        return False
    if status != 201:
        logger.warning(
            "Work-owned notice rejected repo=%s issue=%s status=%s",
            repo_full_name,
            issue,
            status,
        )
        return False
    logger.info("Work-owned notice posted repo=%s issue=%s", repo_full_name, issue)
    return True
