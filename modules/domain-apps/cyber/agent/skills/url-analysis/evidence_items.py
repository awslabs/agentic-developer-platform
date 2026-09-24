"""Collector-owned references to bounded evidence, separate from page coverage.

An intact item is evidence of what was captured, not proof of malicious intent.
Item hashes are checked again before accepting an assessment. Old captures without
these records retain the original conservative rules for partial observations.
"""

from __future__ import annotations

import hashlib
import json


def item_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def inventory_digest(observation):
    return item_digest(
        {
            k: observation.get(k)
            for k in (
                "scripts",
                "forms",
                "visible_text",
                "network_requests",
                "redirects",
                "downloads",
                "counts",
                "screenshot_sha256",
                "evidence_items",
            )
        }
    )


def validate_inventory(observation):
    if "evidence_items" in observation and (
        observation.get("evidence_sha256") != inventory_digest(observation)
        or observation["evidence_items"] != build_evidence_items(observation)
    ):
        raise ValueError("Evidence inventory integrity check failed")


def build_evidence_items(observation):
    items = []

    def add(kind, index, value, complete):
        items.append(
            {
                "id": f"{kind}-{index + 1:03d}",
                "kind": kind,
                "index": index,
                "complete": bool(complete),
                "sha256": item_digest(value),
            }
        )

    counts = observation.get("counts", {})
    text = observation.get("visible_text", "")
    if text:
        add("text", 0, text, counts.get("text_chars", len(text)) <= 20000)
    for i, form in enumerate(observation.get("forms", [])):
        add("form", i, form, form.get("fields_truncated") is False)
    for i, script in enumerate(observation.get("scripts", [])):
        if script.get("inline"):
            add("script", i, script, script.get("truncated") is False)
    for kind, field in (
        ("network", "network_requests"),
        ("redirect", "redirects"),
        ("download", "downloads"),
    ):
        for i, value in enumerate(observation.get(field, [])):
            complete = not value.get("error")
            if kind == "network":
                complete = complete and value.get("status", 0) > 0
            if kind == "redirect":
                complete = complete and value.get("kind") in {"http", "navigation"}
            add(kind, i, value, complete)
    if observation.get("screenshot_sha256"):
        add("screenshot", 0, observation["screenshot_sha256"], True)
    return items


def checked_item(observation, item_id):
    recorded = next(
        (x for x in observation.get("evidence_items", []) if x.get("id") == item_id),
        None,
    )
    expected = next(
        (x for x in build_evidence_items(observation) if x["id"] == item_id), None
    )
    if recorded is None or expected != recorded:
        raise ValueError("Unknown or changed evidence item")
    if not recorded["complete"]:
        raise ValueError("The cited evidence item was truncated or failed")
    return recorded


def partial_evidence_usable(observation):
    """Permit coverage gaps, never a failed page, challenge, or unknown cleanup."""
    allowed_gaps = {
        "page_capture_truncated",
        "network_requests_failed",
        "observation_coverage_limited",
        "connections_truncated",
    }
    return (
        observation.get("status") == "partial"
        and type(observation.get("http_status")) is int
        and 200 <= observation["http_status"] < 400
        and bool(observation.get("screenshot_sha256"))
        and bool(observation.get("screenshot") or observation.get("screenshot_base64"))
        and bool(observation.get("dom_snapshot"))
        and bool(observation.get("evidence_items"))
        and set(observation.get("errors", [])) <= allowed_gaps
        and not any(
            x.get("navigation") for x in observation.get("blocked_requests", [])
        )
    )
