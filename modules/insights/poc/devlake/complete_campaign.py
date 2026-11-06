"""Consume a protected final verdict through the scoped orchestration issue host."""

import hashlib
import re

from provider_evidence import verify


REQUIRED_CHECKS = frozenset(("AC-01", "AC-02", "AC-03", "AC-04",
                             "AC-05", "SCN-01", "SCN-02", "SCN-03", "SCN-04", "SCN-05"))
REVISION = re.compile(r"[a-f0-9]{64}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


def _binding(entry):
    if (not isinstance(entry, dict) or
            not isinstance(entry.get("repository"), str) or
            not REPOSITORY.fullmatch(entry["repository"]) or
            type(entry.get("issue_number")) is not int or entry["issue_number"] < 1 or
            not isinstance(entry.get("requirement_revision"), str) or
            not REVISION.fullmatch(entry["requirement_revision"])):
        raise ValueError("accepted issue binding is incomplete")
    return entry["repository"], entry["issue_number"], entry["requirement_revision"]


def _issue(host, repository, number, revision, closure_key):
    record = host.read_issue(repository, number)
    if (not isinstance(record, dict) or record.get("repository") != repository or
            record.get("issue_number") != number or
            record.get("requirement_revision") != revision):
        raise ValueError("current issue requirement revision changed")
    if record.get("state") not in ("open", "closed"):
        raise ValueError("current issue state is unavailable")
    if record["state"] == "closed" and record.get("closure_key") != closure_key:
        raise ValueError("issue was closed without this protected verdict")
    return record


def consume(run_id, host):
    """Close only admitted, revision-matched issues after authenticated machine acceptance.

    The protected host reads the native verdict and accepted bindings. Its
    close_issue_if_current operation must atomically compare the current
    requirement revision and provider revision digest and store closure_key with
    the close. The protected host reads current provider revisions independently
    before each close. A CLI-supplied
    verdict, ordinary workflow summary or unconditional GitHub PATCH is not a
    valid host implementation.
    """
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("bounded protected run ID required")
    verdict = host.read_final_verdict(run_id)
    bindings = host.read_accepted_issues(run_id)
    if (not isinstance(verdict, dict) or verdict.get("run_id") != run_id or
            verdict.get("phase") != "closure" or verdict.get("status") != "accepted" or
            type(verdict.get("run_attempt")) is not int or verdict["run_attempt"] < 1 or
            not isinstance(verdict.get("receipt_ref"), str) or not verdict["receipt_ref"] or
            not isinstance(verdict.get("acceptance"), dict) or
            not REQUIRED_CHECKS.issubset(verdict["acceptance"]) or
            not verdict["acceptance"] or
            any(value != "pass" for value in verdict["acceptance"].values())):
        raise ValueError("protected final machine verdict is not eligible")
    def current_provider_proof():
        selection = host.read_current_provider_revisions(run_id)
        digest = verify(verdict.get("provider_evidence"), selection, selection.get("source_revision")
                        if isinstance(selection, dict) else None, run_id, verdict["run_attempt"])
        if digest != verdict.get("provider_revisions_digest"):
            raise ValueError("protected provider revision digest differs from current selection")

    current_provider_proof()
    if not isinstance(bindings, list) or not bindings or len(bindings) > 100:
        raise ValueError("accepted issue bindings are unavailable")
    pinned = [_binding(binding) for binding in bindings]
    keys = [(repository, number) for repository, number, _ in pinned]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate accepted issue binding")
    received = verdict.get("requirements")
    if not isinstance(received, list) or len(received) != len(pinned):
        raise ValueError("verdict requirement revisions differ from acceptance")
    verified = [_binding(requirement) for requirement in received]
    if len(set(verified)) != len(verified) or set(verified) != set(pinned):
        raise ValueError("verdict requirement revisions differ from acceptance")
    targets = []
    for repository, number, revision in sorted(pinned):
        closure_key = hashlib.sha256(
            f"{run_id}\0{verdict['run_attempt']}\0{verdict['receipt_ref']}\0{repository}\0{number}\0{revision}".encode()
        ).hexdigest()
        targets.append((repository, number, revision, closure_key))
    for repository, number, revision, closure_key in targets:
        current_provider_proof()
        _issue(host, repository, number, revision, closure_key)
    closed = []
    replayed = []
    for repository, number, revision, closure_key in targets:
        current_provider_proof()
        current = _issue(host, repository, number, revision, closure_key)
        target = {"repository": repository, "issue_number": number}
        if current["state"] == "closed":
            replayed.append(target)
            continue
        try:
            host.close_issue_if_current(repository, number, revision, closure_key,
                                        verdict["provider_revisions_digest"])
        except Exception:
            observed = _issue(host, repository, number, revision, closure_key)
            if observed["state"] != "closed":
                raise
        confirmed = _issue(host, repository, number, revision, closure_key)
        if confirmed["state"] != "closed":
            raise ValueError("issue closure lacks a matching protected receipt")
        closed.append(target)
    return {"scope": "protected-post-verdict", "run_id": run_id,
            "run_attempt": verdict["run_attempt"], "closed": closed, "replayed": replayed}
