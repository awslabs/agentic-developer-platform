"""Quota observability middleware — logs quota refusals. Does NOT enforce.

WHAT THIS USED TO CLAIM, AND WHY THAT WAS A DEFECT
--------------------------------------------------
This middleware set ``X-Quota-Enforcement: active`` on EVERY response, including
responses from paths where no quota decision was made at all. At the time it did
that, the deployment-creation path performed no quota check whatsoever (issue #5671,
A15): the header asserted a control that was not running. It also compiled three
route patterns and never evaluated them, so reading the code suggested a backstop
existed.

That is worse than having no signal. An operator or auditor checking whether
enforcement is on had a header telling them yes, so the gap the header was covering
could not be noticed by looking at the system's own output.

WHY THE HEADER IS NOW DERIVED, NOT ASSERTED
-------------------------------------------
Enforcement happens in the router handlers, which is the right place for it — they
hold the workspace, the request's requested capacity and the transaction the
reservation must be atomic with, none of which middleware can see without re-parsing
the body and re-opening a session.

So this middleware no longer claims anything about paths it cannot vouch for. The
header is set only when the handler actually recorded a quota decision for that
request, via ``request.state``, and the quota service is what sets that. A path with
no quota decision now produces NO enforcement header, which is the honest answer and
is the signal a reviewer can act on.

The unevaluated route patterns are gone rather than left as documentation: a compiled
pattern that nothing matches against reads like a guard.
"""

import logging
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

# Attribute the quota service sets on `request.state` once it has evaluated a quota
# decision for the request. Absent means no decision was made — see module docstring.
QUOTA_DECISION_ATTR = "quota_decision_made"

# Header value used only when a decision was genuinely made for this request.
QUOTA_ENFORCEMENT_HEADER = "X-Quota-Enforcement"


class QuotaEnforcementMiddleware(BaseHTTPMiddleware):
    """Reports whether a quota decision was made, and logs refusals.

    Naming kept for compatibility with the app's middleware registration; the
    enforcement itself lives in the router handlers and the quota service.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Only claim enforcement where a handler actually made a quota decision. The
        # default is silence, not "active": an unchecked path must not look guarded.
        if getattr(request.state, QUOTA_DECISION_ATTR, False):
            response.headers[QUOTA_ENFORCEMENT_HEADER] = "active"

        # Log quota exceeded responses
        if response.status_code == 429:
            quota_type = response.headers.get("X-Quota-Type", "unknown")
            logger.warning(
                "Quota exceeded: path=%s method=%s quota_type=%s",
                request.url.path,
                request.method,
                quota_type,
            )

        return response
