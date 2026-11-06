"""Preserve the reviewer's cause instead of an arbitrary stderr tail."""

import json
import signal
import re


def failure_summary(result) -> str:
    status = f"exit {result.returncode}"
    if result.returncode < 0:
        try:
            status = f"signal {signal.Signals(-result.returncode).name}"
        except ValueError:
            status = f"signal {-result.returncode}"
    message = "Reviewer exited without failure diagnostics"
    for line in reversed((result.stdout or "").splitlines()):
        try:
            document = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(document, dict) or document.get("status") not in {"review_failed", "agent_failed"}:
            continue
        failure = document.get("error")
        if isinstance(failure, dict) and isinstance(failure.get("message"), str) and failure["message"].strip():
            message = failure["message"]
            break
    else:
        stderr = "\n".join(line for line in (result.stderr or "").splitlines() if not line.lstrip().startswith("[TokenManager]")).strip()
        if stderr:
            marker = "agent-codex-reviewer failed"
            index = stderr.rfind(marker)
            message = stderr[index + len(marker):].strip() if index >= 0 else stderr[-1024:]
    return f"Codex reviewer {status}: {message}"[:1024]


def failure_details(result, *, aborted=False) -> dict:
    """Diagnostic hints only. These never grant authority or authorize a retry."""
    message = failure_summary(result).lower()
    category = "unknown"
    if aborted:
        category = "cancelled"
    elif result.returncode == 124:
        category = "deadline"
    elif result.returncode < 0:
        category = "signal"
    else:
        for name, pattern in (
            ("policy", r"policy_expired|execution_policy_refused|budget_exceeded|budget_unavailable|wall_clock"),
            ("provider_refusal", r"high-risk cyber|cybersecurity safety|safety refusal"),
            ("authentication", r"bad credentials|invalid username or token|authentication failed|missing_token"),
            ("transport", r"aborterror|idle timeout.*sse|unterminated string|connection reset"),
            ("stale_head", r"head changed|head moved|projection"),
            ("git_validation", r"diff.*check|conflict marker|whitespace error|ignored.*path"),
            ("inspection", r"inspection.*(incomplete|did not complete)"),
            ("contract", r"envelope|triggering_comment"),
        ):
            if re.search(pattern, message):
                category = name
                break
    # Do not duplicate arbitrary model/tool text (or credentials) in SQL receipts.
    return {"category": category, "exit_code": int(result.returncode)}


def _unique_object(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("ambiguous failure diagnostic")
        document[key] = value
    return document


def budget_stop_reason(result) -> str | None:
    """Read a native SDK's terminal HTTP denial, never model/tool or stderr text.

    Developers use the private failure-report file; reviewers emit the same
    bounded envelope as their final stdout line. Parse the gateway body before
    display truncation, and only read its exact scope field. This restricts
    scheduling; it grants no budget, claim, or additional attempt.
    """
    if result.returncode <= 0 or result.returncode == 124 or not isinstance(result.stdout, str):
        return None
    # The producers cap error.message at 8192 characters. Bound both JSON reads
    # and require the terminal envelope, rather than searching earlier output.
    line = result.stdout[-16385:].rstrip().rsplit("\n", 1)[-1]
    if not line or len(line) > 16384:
        return None
    try:
        document = json.loads(line, object_pairs_hook=_unique_object)
        if not isinstance(document, dict) or document.get("status") not in {"agent_failed", "review_failed"}:
            return None
        error = document.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        if not isinstance(message, str) or len(message) > 8192:
            return None
        prefix = re.match(r"^(?:unexpected status )?402 Payment Required:\s*", message)
        if prefix is None:
            return None
        body, _ = json.JSONDecoder(object_pairs_hook=_unique_object).raw_decode(message, prefix.end())
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(body, dict) or body.get("error") != "budget_exceeded":
        return None
    details = body.get("details", {})
    if not isinstance(details, dict):
        return None
    scope = details.get("scope")
    if scope is None:
        # Older hierarchy denials have no scope discriminator.
        return "hierarchy_cap_exceeded"
    if not isinstance(scope, str):
        return None
    return {
        "run": "run_cap_exceeded",
        "chain": "chain_cap_exceeded",
        "root_user": "root_user_cap_exceeded",
        "person": "person_cap_exceeded",
        "org": "hierarchy_cap_exceeded",
        "team": "hierarchy_cap_exceeded",
        "user": "hierarchy_cap_exceeded",
    }.get(scope)
