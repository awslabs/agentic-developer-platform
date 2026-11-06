"""A fixture's authenticated credential can never stand in for a human."""

import hashlib

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from src.auth.tenant_context import split_token


def fingerprint(request):
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise HTTPException(401, detail={"code": "security_evidence_session_required"})
    token, _ = split_token(value)
    return hashlib.sha256(token.encode()).hexdigest()


async def require_interactive_credential(request, actor, db):
    from src.products.security_evidence_store import event, event_id

    marked = await event(db, actor.org_id, event_id(fingerprint(request), "credential"), "fixture", required=False)
    if marked is not None:
        raise HTTPException(403, detail={"code": "security_evidence_interactive_session_required"})
    await require_human_subject(request, actor.user_id, getattr(actor, "_authenticated_subject_id", ""))


async def require_human_subject(request, *subjects):
    """A selected observer must not be a designated automation subject."""
    runtime = getattr(getattr(request.app, "state", None), "native_test_runtime", None)
    if runtime is not None and runtime.configured:
        if runtime.store is None:
            raise HTTPException(503, detail={"code": "security_evidence_fixture_registry_unavailable"})
        for subject in set(subjects) - {"", None}:
            try:
                designated = await run_in_threadpool(runtime.store.designation, subject)
            except Exception:
                raise HTTPException(503, detail={"code": "security_evidence_fixture_registry_unavailable"}) from None
            if designated is not None:
                raise HTTPException(
                    403,
                    detail={
                        "code": "security_evidence_interactive_session_required",
                        "next_action": (
                            "Select a real Product member who can sign in separately; designated automation accounts cannot approve human evidence."
                        ),
                    },
                )
