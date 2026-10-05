"""Strict, credential-free URL tool inputs shared by service transports."""

import ipaddress
import re
from urllib.parse import urlsplit
from fastapi import HTTPException

FIELDS = {
    "common_crawl_scan": ({"url"}, {"match"}),
    "common_crawl_result": ({"scan_id"}, set()),
    "common_crawl_read": ({"scan_id", "capture_id"}, set()),
    "browser_start": ({"url"}, {"session_key", "profile", "scope"}),
    "browser_step": (
        {"session_id", "view_id", "action"},
        {"candidate_id", "seconds", "url"},
    ),
    "browser_close": ({"session_id"}, set()),
    "browser_inspect": ({"session_id"}, {"section", "offset"}),
}


def validate_url_payload(operation, payload):
    if operation not in FIELDS or not isinstance(payload, dict):
        raise HTTPException(422, "Unknown URL tool")
    required, optional = FIELDS[operation]
    if not required <= payload.keys() or payload.keys() - required - optional:
        raise HTTPException(422, "Invalid URL tool fields")
    for name, value in payload.items():
        if name == "offset":
            if type(value) is not int or not 0 <= value <= 1000000:
                raise HTTPException(422, "Invalid inspection offset")
        elif name == "seconds":
            if type(value) is not int or not 1 <= value <= 15:
                raise HTTPException(422, "Invalid browser wait")
        elif not isinstance(value, str) or not 0 < len(value) <= 2048:
            raise HTTPException(422, "Invalid URL tool value")
    for name in {"scan_id", "session_id", "job_id"} & payload.keys():
        if not re.fullmatch(r"[a-f0-9]{64}", payload[name]):
            raise HTTPException(422, "Invalid URL tool reference")
    if "session_key" in payload and not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", payload["session_key"]
    ):
        raise HTTPException(422, "Invalid browser session key")
    if "capture_id" in payload and not re.fullmatch(
        r"capture-[0-9]{3}", payload["capture_id"]
    ):
        raise HTTPException(422, "Invalid archive capture")
    if payload.get("match", "host") not in {"host", "exact"}:
        raise HTTPException(422, "Invalid archive match")
    if payload.get("profile", "desktop") not in {"desktop", "mobile"} or payload.get(
        "scope", "observed_external"
    ) not in {"host", "observed_external"}:
        raise HTTPException(422, "Invalid browser profile/scope")
    if payload.get("section", "summary") not in {
        "summary",
        "dom",
        "forms",
        "scripts",
        "network",
        "frames",
        "screenshot",
        "choices",
    }:
        raise HTTPException(422, "Invalid evidence section")
    if operation == "browser_step":
        action = payload["action"]
        if action not in {
            "navigate",
            "follow",
            "expand",
            "root",
            "screenshot",
            "back",
            "scroll",
            "wait",
        }:
            raise HTTPException(422, "Invalid browser action")
        if (action in {"follow", "expand"}) != ("candidate_id" in payload):
            raise HTTPException(422, "Select an observed browser candidate")
        if (action == "wait") != ("seconds" in payload):
            raise HTTPException(422, "Wait seconds required only for wait")

        if (action == "navigate") != ("url" in payload):
            raise HTTPException(422, "Navigation URL required only for navigate")


def checked_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise HTTPException(422, "Invalid analysis URL")
    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(
        (".localhost", ".internal", ".local")
    ):
        raise HTTPException(403, "Analysis destination refused")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return  # The guarded browser backend must also validate DNS and redirects.
    if not address.is_global:
        raise HTTPException(403, "Analysis destination refused")
