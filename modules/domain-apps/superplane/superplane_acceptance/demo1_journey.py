"""One explicitly authorized browser phase, preserving uncertain original requests."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from .demo1_browser import PREFIX, PlaywrightBrowserTransport, advance_creation
from .demo1_evidence import EvidenceError
from .demo1_lineage import observe_lineage
from .demo1_report import reference
from .demo1_runtime import RuntimeReader


def advance_browser(
    selected,
    envelope,
    session,
    store,
    *,
    clock=lambda: datetime.now(UTC),
    monotonic=time.monotonic,
):
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

    saved = store.load()
    reader = RuntimeReader(selected, envelope.runtime_target)
    runtime = reader.observe(remaining_ms() / 1000)

    def verify_lineage(checkpoint, original_operation, current_operation):
        return observe_lineage(
            reader,
            checkpoint,
            original_operation,
            current_operation,
            remaining_ms() / 1000,
            now=clock(),
        )

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
                    storage_state=session, service_workers="block"
                )
                page = context.new_page()
                page.set_default_timeout(min(30_000, remaining_ms()))
                page.set_default_navigation_timeout(min(30_000, remaining_ms()))
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
                    verify_lineage=verify_lineage,
                    effects_authorized=True,
                    now=clock(),
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
