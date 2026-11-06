"""Guard a single owner-authorized Security selector; never provision a test target."""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fixtures import ROOT, load_live_fixture


SELECTORS = {
    "setup": "test_clients_and_setup",
    "assessments": "test_real_security_assessments",
    "recovery": "test_isolation_recovery_cleanup",
    "context": "test_context_reuse",
}
JOURNALS = {
    "setup": "journal_file",
    "assessments": "assessment_journal_file",
    "recovery": "journal_file",
    "context": "journal_file",
}


def check_authorization(fixture, scenario, source_sha, evidence_dir, *, now=None, fixture_path=None, environment=None):
    if scenario not in SELECTORS or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("select one bounded scenario from a reviewed source revision")
    permission = fixture.get("authorization", {})
    try:
        expires_at = datetime.fromisoformat(permission["expires_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("current owner authorization must include an expiry") from error
    now = now or datetime.now(UTC)
    if (expires_at.tzinfo is None or not now < expires_at <= now + timedelta(hours=24)
            or permission.get("source_sha") != source_sha
            or not isinstance(permission.get("scenarios"), list)
            or scenario not in permission["scenarios"]):
        raise ValueError("owner authorization has expired or does not match this scenario and revision")
    directory = Path(evidence_dir)
    if (not directory.is_absolute() or directory.is_relative_to(ROOT)
            or not directory.is_dir() or directory.stat().st_mode & 0o077):
        raise ValueError("owned attempt evidence needs a pre-existing private directory outside the checkout")
    directory = directory.resolve(strict=True)
    if directory.is_relative_to(ROOT):
        raise ValueError("evidence directory resolves into the checkout")
    journals = []
    for case in fixture["cases"] if scenario in {"setup", "assessments"} else fixture["cases"][:1]:
        selected = case if scenario in {"setup", "assessments"} else case["recovery" if scenario == "recovery" else "context_reuse"]
        journal = Path(selected[JOURNALS[scenario]])
        if (not journal.is_absolute() or journal.is_symlink()
                or not journal.resolve().is_relative_to(directory)
                or journal.parent.stat().st_mode & 0o077):
            raise ValueError("existing, shared or public attempt journals require reconciliation before live work")
        if journal.exists():
            if scenario != "setup" or fixture_path is None or environment is None:
                raise ValueError("existing attempt journals require exact original manual setup reconciliation")
            from security_manual_setup import private_events, verify_fixture
            identity, fixture_sha256, _ = verify_fixture(fixture, fixture_path, environment)
            references = private_events(Path(str(journal) + ".manual.jsonl"))
            if not references or any(item["release"] != identity or item["fixture_sha256"] != fixture_sha256 for item in references):
                raise ValueError("existing setup journal belongs to another release or fixture")
            started = next((item["started_at"] for item in references if item.get("kind") == "start"), None)
            if started is None or datetime.fromisoformat(started) + timedelta(seconds=fixture["limits"]["deadline_seconds"]) <= now:
                raise ValueError("original setup deadline expired; retained effects require read-only reconciliation")
            if not any(item.get("kind") == "cli_intent" for item in references):
                raise ValueError("original CLI intent is missing; no setup retransmission is authorized")
        journals.append(journal.resolve())
    if len(set(journals)) != len(journals):
        raise ValueError("two Products must not share an attempt journal")
    return fixture


def preflight(scenarios, source_sha, fixture_path, evidence_dir, credentials_json):
    credentials = json.loads(credentials_json)
    if (not isinstance(credentials, dict) or not credentials or any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or not isinstance(value, str) or not value
            for name, value in credentials.items())):
        raise ValueError("private authorization must provide a non-empty environment variable map")
    environment = os.environ.copy()
    if set(credentials) & set(environment):
        raise ValueError("private credentials may not override the runner environment")
    environment.update(credentials)
    previous = os.environ.copy()
    try:
        os.environ.update(credentials)
        fixture = load_live_fixture(fixture_path)
        journals = []
        for scenario in scenarios:
            check_authorization(fixture, scenario, source_sha, evidence_dir, fixture_path=fixture_path, environment=environment)
            for case in fixture["cases"] if scenario in {"setup", "assessments"} else fixture["cases"][:1]:
                selected = case if scenario in {"setup", "assessments"} else case[
                    "recovery" if scenario == "recovery" else "context_reuse"]
                journals.append(Path(selected[JOURNALS[scenario]]).resolve())
        if len(set(journals)) != len(journals):
            raise ValueError("separate Security scenarios require separate private journals")
    finally:
        os.environ.clear()
        os.environ.update(previous)
    return fixture, environment


def run(scenario, source_sha, fixture_path, evidence_dir, credentials_json):
    fixture, environment = preflight((scenario,), source_sha, fixture_path, evidence_dir, credentials_json)
    run_id = environment.get("GITHUB_RUN_ID", "")
    attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    if run_id.isdecimal() and attempt.isdecimal():
        stem = f"security-{run_id}-{attempt}-{scenario}"
        completed = list(Path(evidence_dir).glob(stem + "*.json"))
        if completed:
            raise ValueError("this selector already passed in the original run; use independent readback without repeating setup")
    environment["ADP_L5_SECURITY_LIVE_CONFIG"] = fixture_path
    environment["ADP_L5_SECURITY_LIVE_ENABLED"] = "I_ACCEPT_PAID_TESTS"
    environment["PYTHONPATH"] = (str(ROOT / "modules/L5-services/src") + os.pathsep
                                 + str(ROOT / "modules/L5-services/tests/live"))
    node = str(ROOT / "modules/L5-services/tests/e2e/test_l5_security_acceptance.py")
    deadline = sum(case["deadline_seconds"] if "deadline_seconds" in case else fixture["limits"]["deadline_seconds"]
                   for case in (fixture["cases"] if scenario in {"setup", "assessments"} else fixture["cases"][:1]))
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", node + "::" + SELECTORS[scenario]],
                            env=environment, cwd=ROOT, capture_output=True, timeout=deadline + 60, check=False)
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    filename = (f"security-{run_id}-{attempt}-{scenario}.log" if run_id.isdecimal() and attempt.isdecimal()
                else f"security-{uuid4().hex}-{scenario}.log")
    if (Path(evidence_dir).resolve() / filename).exists():
        # Keep every actual prior attempt log. A continuation is separately
        # attributed to this same run and never overwrites an earlier result.
        filename = filename.replace(".log", f"-reconcile-{uuid4().hex}.log")
    descriptor = os.open(Path(evidence_dir).resolve() / filename,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    log_content = result.stdout + b"\n" + result.stderr
    with os.fdopen(descriptor, "wb") as output:
        output.write(log_content)
        output.flush()
        os.fsync(output.fileno())
    if result.returncode != 0 or b" skipped" in result.stdout or b" passed" not in result.stdout:
        raise RuntimeError("live selector did not pass; inspect private journal and runner-local output before retry")
    if run_id.isdecimal() and attempt.isdecimal():
        receipt = {"scenario": scenario, "run_id": run_id, "run_attempt": attempt,
                   "selector_node": "modules/L5-services/tests/e2e/test_l5_security_acceptance.py::" + SELECTORS[scenario],
                   "source_revision": source_sha,
                   "fixture_sha256": hashlib.sha256(Path(fixture_path).read_bytes()).hexdigest(),
                   "log_sha256": hashlib.sha256(log_content).hexdigest(),
                   "result": "passed", "observed_at": datetime.now(UTC).isoformat()}
        receipt_path = Path(evidence_dir).resolve() / filename.replace(".log", ".json")
        descriptor = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as destination:
            json.dump(receipt, destination, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
    return result.returncode


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit("usage: run_security_live.py {preflight|SCENARIO} REVIEWED_SHA PRIVATE_FIXTURE PRIVATE_EVIDENCE_DIR")
    try:
        if sys.argv[1] == "preflight":
            preflight(SELECTORS, *sys.argv[2:], os.environ["ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON"])
        else:
            run(*sys.argv[1:], os.environ["ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON"])
    except (ValueError, RuntimeError, KeyError, OSError, subprocess.TimeoutExpired):
        raise SystemExit("live selector did not run or pass; inspect private runner evidence") from None
    print("live selector passed; independent evidence still requires owner acceptance")
