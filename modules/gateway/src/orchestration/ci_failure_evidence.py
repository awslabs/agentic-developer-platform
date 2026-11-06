"""Bounded, repository-neutral GitHub failure evidence for the assigned reviewer."""

from __future__ import annotations

import asyncio
import os
import re
from urllib.parse import urlsplit

import httpx

from src.agentauth.actions_logs import redact_text

from .merge_evidence import EvidenceUnavailableError

MAX_DETAILS = 4000
MAX_LOG_BYTES = 2 * 1024 * 1024
TAIL_BYTES = 256 * 1024
RUN_LINK = re.compile(r"/actions/runs/([1-9][0-9]*)/job/([1-9][0-9]*)\Z")
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
SIGNED_URL = re.compile(r"https?://[^\s\"'<>?]+\?[^\s\"'<>]+", re.IGNORECASE)
AUTH_HEADER = re.compile(r"""\b(?:Authorization|Proxy-Authorization)["']?\s*[:=]\s*[^\r\n]+""", re.IGNORECASE)
SECRET_ENV = re.compile(r"(?:SECRET|TOKEN|PASSWORD|API[_-]?KEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY|DATABASE_URL|REDIS_URL)", re.IGNORECASE)
FAILURE_TEXT = re.compile(r"\b(?:FAIL(?:ED|URES?)?|ERRORS?)\b|\berror(?:\[[^\]\r\n]{1,40}\])?:")
EXCEPTION_TEXT = re.compile(r"\bTraceback\b|\b\w*(?:Error|Exception):")


def redact_details(value, *, token="", limit=MAX_DETAILS, tail=False):
    clean = ESCAPES.sub("", value)
    clean = SIGNED_URL.sub("[REDACTED_URL]", clean)
    clean = AUTH_HEADER.sub("Authorization: [REDACTED]", clean)
    secrets = [token, *(value for key, value in os.environ.items() if SECRET_ENV.search(key) and len(value) >= 6)]
    for secret in sorted({secret for secret in secrets if len(secret) >= 6}, key=len, reverse=True):
        clean = clean.replace(secret, "[REDACTED]")
    clean = redact_text(clean, max_bytes=TAIL_BYTES + 20 * 700 + 1000)
    return clean[-limit:] if tail else clean[:limit]


def failure_excerpt(value, *, limit):
    """Keep diagnostic context before cleanup output, without interpreting CI status."""
    if len(value) <= limit:
        return value
    windows = []
    # Prefer explicit test/compiler failures over incidental cleanup tracebacks.
    pattern = FAILURE_TEXT if FAILURE_TEXT.search(value) else EXCEPTION_TEXT
    for match in pattern.finditer(value):
        if windows and match.start() < windows[-1][1]:
            continue
        start = value.rfind("\n", 0, max(0, match.start() - 200)) + 1
        # A long unbroken line must not consume the context budget before the failure.
        start = max(start, match.start() - 400)
        windows.append((start, min(len(value), match.end() + 1800)))
        if len(windows) == 3:
            break
    if not windows:
        return value[-limit:]
    separator = "\n[... omitted log lines ...]\n"
    budget = (limit - len(separator) * (len(windows) - 1)) // len(windows)
    return separator.join(value[start : min(end, start + budget)] for start, end in windows)


def _run_job_ids(repo, check):
    value = check.get("details_url")
    if not isinstance(value, str):
        return None
    try:
        link = urlsplit(value)
    except ValueError:
        return None
    if link.scheme != "https" or link.netloc != "github.com" or not link.path.startswith("/" + repo + "/"):
        return None
    match = RUN_LINK.fullmatch(link.path[len("/" + repo) :])
    return (int(match[1]), int(match[2])) if match and not link.query and not link.fragment else None


async def _get(observer, path):
    try:
        return await observer.get(path, kind="failed_check_evidence")
    except (EvidenceUnavailableError, httpx.HTTPError):
        return None


async def failure_details(observer, repo, check, head_sha, *, actions_available=True):
    """Return annotations and a verified Actions job, never a name-matched job."""
    root = f"/repos/{repo}"
    annotations = await _get(observer, f"{root}/check-runs/{check['id']}/annotations?per_page=20&page=1")
    parts = []
    failed_step = ""
    log_excerpt = ""
    if isinstance(annotations, list):
        for annotation in annotations[:20]:
            if not isinstance(annotation, dict) or annotation.get("annotation_level") not in {"failure", "warning"}:
                continue
            path = str(annotation.get("path") or "")[:200]
            message = str(annotation.get("message") or "")[:500]
            if message:
                parts.append(f"{path}: {message}")

    reason = "job_not_correlated"
    identifiers = _run_job_ids(repo, check)
    suite = check.get("check_suite") or {}
    if not actions_available:
        reason = "actions_read_unavailable"
    elif identifiers and isinstance(suite, dict) and type(suite.get("id")) is int:
        run_id, job_id = identifiers
        run = await _get(observer, f"{root}/actions/runs/{run_id}")
        job = await _get(observer, f"{root}/actions/jobs/{job_id}") if run else None
        if not isinstance(run, dict) or not isinstance(job, dict):
            reason = "job_metadata_unavailable"
        elif (
            run.get("head_sha") != head_sha
            or run.get("check_suite_id") != suite["id"]
            or job.get("head_sha") != head_sha
            or job.get("run_id") != run_id
            or job.get("id") != job_id
            or job.get("check_run_url") != f"https://api.github.com{root}/check-runs/{check['id']}"
            or job.get("run_attempt") != run.get("run_attempt")
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] < 1
        ):
            reason = "job_not_correlated"
        else:
            steps = job.get("steps") or []
            failed = next((step for step in steps if isinstance(step, dict) and step.get("conclusion") == "failure"), None)
            if failed:
                failed_step = f"Failed step: {str(failed.get('name') or '')[:200]}"
            log, reason = await download_job_log(observer.client, observer.headers, repo, job_id)
            if log:
                log_excerpt = log
            elif not reason:
                reason = "job_log_empty"

    if reason:
        parts.append(f"Job log unavailable ({reason}).")
    token = observer.headers.get("Authorization", "").removeprefix("Bearer ")
    step_suffix = "\n" + redact_details(failed_step, token=token, limit=250) if failed_step else ""
    prefix_limit = (900 if log_excerpt else MAX_DETAILS) - len(step_suffix)
    prefix = redact_details("\n".join(parts), token=token, limit=prefix_limit) + step_suffix
    details = (
        prefix + "\n" + failure_excerpt(redact_details(log_excerpt, token=token, limit=TAIL_BYTES), limit=MAX_DETAILS - len(prefix) - 1)
        if log_excerpt
        else prefix
    )
    return {
        "details": details,
        "details_source": "annotations_and_job_log" if not reason else "annotations",
        **({"details_unavailable_reason": reason, "details_url": f"https://github.com/{repo}/runs/{check['id']}"} if reason else {}),
    }


async def download_job_log(client, headers, repo, job_id):
    try:
        return await asyncio.wait_for(_download_job_log(client, headers, repo, job_id), timeout=20)
    except TimeoutError:
        return "", "job_log_unavailable"


async def _download_job_log(client, headers, repo, job_id):
    """Download only from the API and a vetted signed-artifact endpoint."""
    api_url = f"https://api.github.com/repos/{repo}/actions/jobs/{job_id}/logs"
    try:
        async with client.stream("GET", api_url, headers=headers, follow_redirects=False) as response:
            if response.status_code in {403, 404}:
                return "", "actions_read_unavailable" if response.status_code == 403 else "job_log_unavailable"
            if response.status_code not in {301, 302, 303, 307, 308}:
                return "", "job_log_unavailable"
            destination = response.headers.get("location", "")
        link = urlsplit(destination)
        if (
            link.scheme != "https"
            or link.username
            or link.password
            or link.port not in (None, 443)
            or link.fragment
            or not (
                link.hostname in {"pipelines.actions.githubusercontent.com", "results-receiver.actions.githubusercontent.com"}
                or re.fullmatch(r"productionresultssa[0-9]+\.blob\.core\.windows\.net", link.hostname or "")
            )
        ):
            return "", "unapproved_log_redirect"
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as signed_client:
            async with signed_client.stream("GET", destination, headers={"Accept": "text/plain"}) as response:
                if response.status_code != 200:
                    return "", "job_log_unavailable"
                tail = bytearray()
                transferred = 0
                async for chunk in response.aiter_raw():
                    transferred += len(chunk)
                    if transferred > MAX_LOG_BYTES:
                        return "", "job_log_too_large"
                    tail.extend(chunk)
                    if len(tail) > TAIL_BYTES:
                        del tail[:-TAIL_BYTES]
        return tail.decode("utf-8", errors="replace"), None
    except (httpx.HTTPError, ValueError):
        return "", "job_log_unavailable"
