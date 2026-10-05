"""Bounded one-shot capture using the same direct session lifecycle as investigations."""

import sys
from pathlib import Path

from case_capture import validate_options
from isolated_browser import ProcessActor


def capture(operation, payload):
    from research_case import _validate_input

    _validate_input(payload["url"])
    validate_options(payload)
    if operation not in {"capture", "analyze"}:
        raise ValueError("Unsupported direct capture operation")
    request = {
        "url": payload["url"],
        "profile": payload.get("profile", "desktop"),
        "wait_seconds": payload.get("wait_seconds", 0),
        "timeout_ms": payload.get("timeout_ms", 30000),
        "_wait_until": payload.get("wait_until", "domcontentloaded"),
        "_context_options": {
            "ignore_https_errors": payload.get("ignore_https_errors", False)
        },
    }
    actor = ProcessActor(
        {"_capture": request},
        None,
        lambda _: None,
        command=[
            sys.executable,
            str(Path(__file__).with_name("isolated_browser.py")),
            "--worker",
            "--native",
        ],
    )
    try:
        result = actor.initial()
    except Exception as error:
        from browser_client import BrowserBrokerError

        raise BrowserBrokerError(
            str(error),
            code=getattr(error, "code", "browser_failed"),
            cleanup=getattr(error, "cleanup", None),
            browser_start_unattempted=getattr(
                error, "browser_start_unattempted", False
            ),
        ) from error
    finally:
        actor.abort()
    if operation == "capture":
        return result
    observation = result["observations"][0]
    return {
        **observation,
        "session_id": result["session_id"],
        "cleanup_status": result["cleanup_status"],
        "refusals": observation.get("blocked_requests", []),
        "frame_navigations": [
            e.get("url", "") for e in observation.get("timeline", [])
        ],
    }
