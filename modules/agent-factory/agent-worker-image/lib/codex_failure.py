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
