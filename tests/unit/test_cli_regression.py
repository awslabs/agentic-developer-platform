"""Nightly execution, revision binding and failure-propagation contracts."""

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from tests.e2e.cli_regression import prepare, report
from tests.e2e.cli_uplift import config, ports

ROOT = Path(__file__).resolve().parents[2]
SHA = "a" * 40
CHILDREN = (
    "eval-cli-onboarding.yml",
    "eval-budget-ratelimit.yml",
    "eval-cli-uplift.yml",
)


def workflow(name):
    document = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    return document, document.get("on", document.get(True))


def outcomes(**overrides):
    return {key: {"result": overrides.get(key, "success")} for key in report.REQUIRED}


def test_complete_success_requires_a_bound_revision():
    text, code = report.render(outcomes(), SHA)
    assert code == 0
    assert "**PASS**" in text
    assert SHA in text


@pytest.mark.parametrize("job", report.REQUIRED)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "unknown"])
def test_one_incomplete_job_cannot_be_hidden_by_other_successes(job, result):
    text, code = report.render(outcomes(**{job: result}), SHA)
    assert code == 1
    assert "FAIL / INCOMPLETE" in text
    assert "**PASS**" not in text


def test_missing_suite_cannot_pass():
    jobs = outcomes()
    del jobs["ec2"]
    text, code = report.render(jobs, SHA)
    assert code == 1
    assert "missing" in text


@pytest.mark.parametrize("revision", ["", "main", "a" * 7, "a" * 40 + "\n", None])
def test_unbound_revision_cannot_pass(revision):
    text, code = report.render(outcomes(), revision)
    assert code == 1
    assert "unverified" in text


def test_summary_cli_preserves_failure_and_appends_evidence(tmp_path, monkeypatch):
    target = tmp_path / "summary.md"
    target.write_text("Existing evidence\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    monkeypatch.setenv("CLI_REGRESSION_REVISION", SHA)
    monkeypatch.setenv("CLI_REGRESSION_JOBS", json.dumps(outcomes(ec2="failure")))
    assert report.main() == 1
    assert target.read_text().startswith("Existing evidence\n")
    assert "including recovery) | failure" in target.read_text()


class Aws:
    def __init__(self, account="879318057152", tags=None):
        self.account = account
        self.tags = tags if tags is not None else [SHA, "latest"]
        self.calls = []

    def call(self, service, operation, **kwargs):
        self.calls.append((service, operation))
        if (service, operation) == ("sts", "get_caller_identity"):
            return {"Account": self.account}
        if (service, operation) == ("lambda", "get_function"):
            return {"Code": {"ResolvedImageUri": "example/adp-gateway@sha256:abc"}}
        if (service, operation) == ("ecr", "describe_images"):
            return {"imageDetails": [{"imageTags": self.tags}]}
        raise AssertionError(f"Unexpected AWS operation: {service}.{operation}")


class Http:
    def __init__(self, health=None):
        self.health = health or {"status": "healthy"}

    def get(self, url, **kwargs):
        assert url.endswith("/api/health")
        return 200, self.health


def test_snapshot_uses_deployment_evidence_instead_of_stale_example_sha():
    cfg = config.load(config.EXAMPLE_PATH)
    assert cfg["expected_revision"] != SHA
    aws = Aws()
    assert prepare.snapshot(cfg, aws, Http()) == (SHA, "deployment_image_tag")
    assert aws.calls == [
        ("sts", "get_caller_identity"),
        ("lambda", "get_function"),
        ("ecr", "describe_images"),
    ]


def test_wrong_account_fails_before_reading_the_deployment():
    aws = Aws(account="123456789012")
    with pytest.raises(ValueError, match="dev account"):
        prepare.snapshot(config.load(config.EXAMPLE_PATH), aws, Http())
    assert aws.calls == [("sts", "get_caller_identity")]


@pytest.mark.parametrize("tags", [["latest"], [SHA, "b" * 40]])
def test_absent_or_ambiguous_deployed_revision_fails(tags):
    with pytest.raises(ports.PortError, match="exactly one"):
        prepare.snapshot(config.load(config.EXAMPLE_PATH), Aws(tags=tags), Http())


def test_short_health_revision_cannot_become_a_revision_pin():
    with pytest.raises(ValueError, match="full commit SHA"):
        prepare.snapshot(
            config.load(config.EXAMPLE_PATH), Aws(), Http({"revision": "abc1234"})
        )


def test_only_the_parent_schedules_and_all_suites_share_one_live_lock():
    parent, triggers = workflow("nightly-cli-regression.yml")
    assert triggers["schedule"] == [{"cron": "0 5 * * *"}]
    assert "workflow_dispatch" in triggers
    locks = []
    for name in CHILDREN:
        child, child_triggers = workflow(name)
        assert "schedule" not in child_triggers
        assert "workflow_call" in child_triggers
        assert "workflow_dispatch" in child_triggers
        assert child["concurrency"]["cancel-in-progress"] is False
        locks.append(child["concurrency"]["group"])
    assert len(set(locks)) == 1
    assert parent["concurrency"]["group"] != locks[0]  # no parent/child deadlock
    assert parent["concurrency"]["cancel-in-progress"] is False


def test_suites_are_serial_but_failure_does_not_skip_later_suites():
    jobs = workflow("nightly-cli-regression.yml")[0]["jobs"]
    assert jobs["onboarding"]["needs"] == "prepare"
    assert set(jobs["budgets"]["needs"]) == {"prepare", "onboarding"}
    assert set(jobs["ec2"]["needs"]) == {"prepare", "budgets"}
    for name, filename in zip(("onboarding", "budgets", "ec2"), CHILDREN):
        job = jobs[name]
        assert job["uses"] == "./.github/workflows/" + filename
        # Explicit status function disables GitHub's implicit success() gate.
        assert "!cancelled()" in job["if"]
        assert "needs.prepare.result == 'success'" in job["if"]
        assert "continue-on-error" not in job
    assert jobs["ec2"]["with"]["suites"] == "full"
    assert "needs.onboarding.outputs.cleanup_ok == 'true'" in jobs["budgets"]["if"]
    assert "needs.budgets.outputs.cleanup_ok == 'true'" in jobs["ec2"]["if"]
    assert jobs["ec2"]["with"]["expected_revision"] == (
        "${{ needs.prepare.outputs.revision }}"
    )
    assert set(jobs["summary"]["needs"]) == set(report.REQUIRED)
    assert "always()" in jobs["summary"]["if"]


def test_prs_cannot_reach_live_jobs_and_standalone_refs_are_guarded():
    jobs = workflow("nightly-cli-regression.yml")[0]["jobs"]
    assert "github.event_name != 'pull_request'" in jobs["prepare"]["if"]
    assert "github.ref == 'refs/heads/main'" in jobs["prepare"]["if"]
    assert "github.event_name != 'pull_request'" in jobs["summary"]["if"]
    for name in CHILDREN[:2]:
        document, triggers = workflow(name)
        job = document["jobs"]["eval"]
        assert triggers["workflow_call"]["outputs"]["cleanup_ok"]["value"] == (
            "${{ jobs.eval.outputs.cleanup_ok }}"
        )
        assert (
            job["outputs"]["cleanup_ok"] == "${{ steps.cleanup.outcome == 'success' }}"
        )
        assert job["if"] == "github.event_name != 'pull_request'"
        assert job["steps"][0]["name"] == "Refuse an untrusted ref"
        assert "refs/heads/main|refs/tags/*)" in job["steps"][0]["run"]
        cleanup = next(
            s for s in job["steps"] if s.get("name") == "Cleanup sweep (always)"
        )
        assert "always()" in cleanup["if"]
        assert "steps.trusted.outcome == 'success'" in cleanup["if"]
        assert cleanup["id"] == "cleanup"
        assert "|| true" not in cleanup["run"]


def test_only_non_secret_job_outcomes_are_sent_to_combined_summary():
    job = workflow("nightly-cli-regression.yml")[0]["jobs"]["summary"]
    step = next(
        s for s in job["steps"] if s.get("name") == "Publish the combined verdict"
    )
    assert step["env"] == {
        "CLI_REGRESSION_JOBS": "${{ toJSON(needs) }}",
        "CLI_REGRESSION_REVISION": "${{ needs.prepare.outputs.revision }}",
    }
    assert "python -m tests.e2e.cli_regression.report" in step["run"]


@pytest.mark.parametrize("suite", ["cli-onboarding", "budget-ratelimit"])
@pytest.mark.parametrize("pod_exit", [0, 1])
def test_cleanup_exit_status_includes_pod_deletion_failure(tmp_path, suite, pod_exit):
    result = run_cleanup(tmp_path, suite, pod_exit=pod_exit)
    assert result.returncode == pod_exit, result.stdout + result.stderr
    assert "SUMMARY" in result.stdout


def test_budget_cleanup_keeps_sweeping_after_database_failure(tmp_path):
    result = run_cleanup(tmp_path, "budget-ratelimit", db_exit=1)
    assert result.returncode == 1
    assert "tagged budget configs" in result.stdout
    assert "tagged rate-limit configs" in result.stdout
    assert "tagged organizations" in result.stdout
    assert "POD_DELETE" in result.stdout
    assert "SUMMARY" in result.stdout


def run_cleanup(tmp_path, suite, *, pod_exit=0, db_exit=0):
    source = (ROOT / "platform/evals" / suite / "run-eval.sh").read_text()
    function = re.search(r"^run_cleanup\(\) \{\n.*?^\}", source, re.M | re.S).group()
    # Exercise the real EXIT/cleanup function with transport-only substitutes.
    # No real API, DB or pod operation is reachable through these shell helpers.
    setup = f"""
set -uo pipefail
FAILURES=0
WORKDIR="$1"
POD_LABEL_APP=eval-test
POD_NAMESPACE=test
EVAL_USER_PREFIX=eval-bgt
U1=user1 U2=user2 U3=user3 A1=admin X1=other
trace() {{ :; }}
log() {{ echo "$*"; }}
fail() {{ echo "FAIL: $*"; FAILURES=$((FAILURES + 1)); }}
state_get() {{ :; }}
write_summary() {{ echo "SUMMARY failures=$FAILURES"; }}
laptop_pod_delete() {{ echo POD_DELETE; return {pod_exit}; }}
delete_seeded_user() {{ echo "DELETE_USER $*"; }}
h_psql() {{ return {db_exit}; }}
"""
    return subprocess.run(
        ["bash", "-c", setup + function + "\ntrue\nrun_cleanup\n", "_", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_codex_receives_valid_config_without_the_unsupported_search_tool(tmp_path):
    # The legacy full dry-run deliberately simulates an unavailable proxy, so it
    # never reaches C10. Exercise C10's actual writer and parse the resulting TOML.
    source = (ROOT / "platform/evals/cli-onboarding/run-eval.sh").read_text()
    writer = re.search(
        r'  cat > "\$WORKDIR/codex-config.toml" <<TOML\n.*?^TOML',
        source,
        re.M | re.S,
    ).group()
    subprocess.run(
        [
            "bash",
            "-c",
            'WORKDIR="$1"\nEVAL_CODEX_MODEL=openai.gpt-5.6-sol\nPROXY_PORT=54321\n'
            + writer,
            "_",
            str(tmp_path),
        ],
        check=True,
        timeout=10,
    )
    document = tomllib.loads((tmp_path / "codex-config.toml").read_text())
    assert document["web_search"] == "disabled"
    assert document["model"] == "openai.gpt-5.6-sol"
    provider = document["model_providers"][document["model_provider"]]
    assert provider["base_url"] == "http://127.0.0.1:54321/openai/v1"
    assert provider["wire_api"] == "responses"
    assert provider["env_key"] == "ADP_GATEWAY_DUMMY"


def test_concurrent_budget_requests_keep_their_own_response_bodies(tmp_path):
    source = (ROOT / "platform/evals/budget-ratelimit/run-eval.sh").read_text()
    function = re.search(r"^laptop_call\(\) \{\n.*?^\}", source, re.M | re.S).group()
    setup = r"""
set -euo pipefail
WORKDIR="$1"
POD_WORKDIR="$WORKDIR/pod"
API=https://gateway.example.test/api
mkdir -p "$POD_WORKDIR"
anthropic_body() { echo '{}' > "$1"; }
laptop_put_file() { mkdir -p "$(dirname "$2")"; cp "$1" "$2"; }
laptop_get_file() { cp "$1" "$2"; }
laptop_http_post_json() {
  # Each transport receives its own response. Both writes complete before either
  # caller reads back, exposing any shared pod response filename deterministically.
  printf '%s' "$out_local" > "$4"
  touch "$out_local.ready"
  local n
  for n in $(seq 1 100); do
    if [ -f "$WORKDIR/one.json.ready" ] && [ -f "$WORKDIR/two.json.ready" ]; then
      printf '200'
      return 0
    fi
    sleep 0.01
  done
  return 1
}
"""
    invoke = r"""
laptop_call claude U2 "$WORKDIR/one.json" >/dev/null & first=$!
laptop_call claude U2 "$WORKDIR/two.json" >/dev/null & second=$!
wait "$first"
wait "$second"
"""
    subprocess.run(
        ["bash", "-c", setup + function + invoke, "_", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    for name in ("one.json", "two.json"):
        target = tmp_path / name
        assert target.read_text() == str(target)
