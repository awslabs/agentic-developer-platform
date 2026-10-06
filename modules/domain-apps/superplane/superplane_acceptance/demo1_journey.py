"""One explicitly authorized browser phase, preserving uncertain original requests."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from .demo1_browser import PREFIX, PlaywrightBrowserTransport, advance_creation
from .demo1_evidence import EvidenceError
from .demo1_report import reference
from .demo1_runtime import RuntimeReader
from .demo1_session import browser_state_parts, restore_browser_session


def advance_browser(
    selected,
    envelope,
    session,
    store,
    *,
    continuation_store=None,
    cleanup_store=None,
    review_retirement_access=False,
    clock=lambda: datetime.now(UTC),
    monotonic=time.monotonic,
):
    if review_retirement_access and continuation_store is not None:
        raise EvidenceError("journey: retirement review cannot advance a continuation")
    if cleanup_store is not None and (
        continuation_store is not None or review_retirement_access
    ):
        raise EvidenceError("journey: cleanup preparation must run as a separate phase")
    if envelope.runtime_target is None or store.version != "demo1-checkpoint-v3":
        raise EvidenceError("journey: runtime-bound execution checkpoint required")
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise EvidenceError(
            "journey: Playwright and its Chromium runtime are required"
        ) from None
    expires = monotonic() + envelope.max_runtime_seconds

    def remaining_ms():
        remaining = min(
            expires - monotonic(), (selected.deadline - clock()).total_seconds()
        )
        if clock() < selected.authorized_at or remaining <= 0:
            raise EvidenceError(
                "journey: authorized runtime exhausted; retain original request"
            )
        return max(1, int(remaining * 1000))

    storage_state, session_storage = browser_state_parts(session, envelope.origin)
    saved = store.load()
    if (
        continuation_store is not None
        or cleanup_store is not None
        or review_retirement_access
    ) and (saved is None or not saved.submitted):
        raise EvidenceError(
            "journey: original submitted creation required before lifecycle follow-up"
        )
    reader = RuntimeReader(selected, envelope.runtime_target)
    runtime = reader.observe(remaining_ms() / 1000)

    if runtime.get("status") != "OBSERVED" or runtime.get("release_ref") != reference(
        envelope.runtime_target.release_id
    ):
        raise EvidenceError(
            "journey: exact runtime observation required before browser effects"
        )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, timeout=remaining_ms())
            try:
                context = browser.new_context(
                    storage_state=storage_state, service_workers="block"
                )
                page = context.new_page()
                page.set_default_timeout(min(30_000, remaining_ms()))
                page.set_default_navigation_timeout(min(30_000, remaining_ms()))
                restore_browser_session(
                    page, envelope.origin, session_storage, timeout=remaining_ms()
                )
                page.goto(
                    envelope.origin + "/superplane",
                    wait_until="domcontentloaded",
                    timeout=remaining_ms(),
                )
                transport = PlaywrightBrowserTransport(
                    page,
                    envelope.origin,
                    release_id=envelope.runtime_target.release_id,
                    remaining_ms=remaining_ms,
                    selected=selected,
                )
                status, _ = transport.request("GET", PREFIX + "/capabilities")
                if status != 200:
                    raise EvidenceError("journey: public release binding unavailable")
                page.get_by_role("heading", name="Workspaces").wait_for(
                    state="visible", timeout=remaining_ms()
                )
                _, result = advance_creation(
                    selected,
                    transport,
                    origin=envelope.origin,
                    checkpoint=saved,
                    persist=store.save,
                    restore_unsent=store.restore_unsent,
                    preview_retirement=continuation_store is None
                    and cleanup_store is None
                    and not review_retirement_access,
                    effects_authorized=True,
                    now=clock(),
                )
                if continuation_store is not None and result.get("creation_observed"):
                    from .demo1_continuation import advance_continuation

                    result["continuation"] = advance_continuation(
                        selected,
                        envelope,
                        transport,
                        continuation_store,
                        clock=clock,
                        verified_source=result["operation_ref"],
                    )
                if review_retirement_access:
                    from .demo1_retirement import review_access

                    result["retirement_access"] = review_access(
                        selected, envelope, transport, saved, result, now=clock()
                    )
                if cleanup_store is not None:
                    from .demo1_cleanup import advance_cleanup

                    result["cleanup_preparation"] = advance_cleanup(
                        selected,
                        envelope,
                        transport,
                        cleanup_store,
                        result,
                        clock=clock,
                    )
                    if result["cleanup_preparation"].get("state") == "succeeded":
                        from .demo1_cleanup_evidence import observe_cleanup
                        from .demo1_teardown import read_teardown_review

                        from .demo1_browser import _response

                        review = _response(
                            transport,
                            "POST",
                            PREFIX
                            + f"/workspaces/{saved.workspace_id}/retirement/preview",
                            {"operation_id": saved.retirement_request_id},
                        )
                        result["cleanup_preparation"]["artifact"] = observe_cleanup(
                            reader,
                            cleanup_store,
                            result["cleanup_preparation"],
                            remaining_ms() / 1000,
                            now=clock(),
                            review=review,
                        )
                        result["cleanup_preparation"]["reason"] = (
                            "authenticated historical preparation and canonical deletion request verified; current provider state and deletion remain unverified"
                        )
                        _, result["retirement_review"] = read_teardown_review(
                            selected,
                            envelope,
                            transport,
                            cleanup_store,
                            result,
                            result["cleanup_preparation"]["artifact"],
                            clock=clock,
                            review=review,
                        )
                remaining_ms()
            finally:
                browser.close()
    except EvidenceError:
        raise
    except (PlaywrightError, OSError, RuntimeError, ValueError):
        raise EvidenceError(
            "journey: browser response unavailable; retain original checkpoint and request"
        ) from None
    return {
        "runtime": runtime,
        "public_release": {
            "status": "OBSERVED",
            "release_ref": reference(envelope.runtime_target.release_id),
        },
        "browser": result,
        "reason": "browser phase attempted; provider evidence and retirement admission remain unverified",
    }
