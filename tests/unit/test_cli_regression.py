"""Nightly execution, revision binding and failure-propagation contracts."""

import json
import os
import sqlite3
import sys
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
    text, code = report.render(outcomes(), SHA, SHA)
    assert code == 0
    assert "**PASS**" in text
    assert SHA in text


@pytest.mark.parametrize("job", report.REQUIRED)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "unknown"])
def test_one_incomplete_job_cannot_be_hidden_by_other_successes(job, result):
    text, code = report.render(outcomes(**{job: result}), SHA, SHA)
    assert code == 1
    assert "FAIL / INCOMPLETE" in text
    assert "**PASS**" not in text


def test_missing_suite_cannot_pass():
    jobs = outcomes()
    del jobs["ec2"]
    text, code = report.render(jobs, SHA, SHA)
    assert code == 1
    assert "missing" in text


@pytest.mark.parametrize("revision", ["", "main", "a" * 7, "a" * 40 + "\n", None])
def test_unbound_revision_cannot_pass(revision):
    text, code = report.render(outcomes(), revision, SHA)
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
    assert jobs["ec2"]["with"]["suites"] == "${{ inputs.ec2_scope || 'login' }}"
    assert jobs["ec2"]["with"]["resolve_revision"] is True
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
        "CLI_REGRESSION_EC2_REVISION": "${{ needs.ec2.outputs.revision }}",
        "CLI_REGRESSION_EC2_SCOPE": "${{ inputs.ec2_scope || 'login' }}",
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


def run_cleanup(tmp_path, suite, *, pod_exit=0, db_exit=0, prior_exit=0):
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
        [
            "bash",
            "-c",
            setup + function + f"\n(exit {prior_exit})\nrun_cleanup\n",
            "_",
            str(tmp_path),
        ],
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


@pytest.mark.parametrize("revision", ["", "main", "a" * 7, None])
def test_ec2_must_publish_its_own_verified_revision(revision):
    text, code = report.render(outcomes(), SHA, revision)
    assert code == 1
    assert "EC2 pinned revision: `unverified`" in text


def test_key_scenarios_pass_without_claiming_full_acceptance():
    text, code = report.render(outcomes(), SHA, "b" * 40)
    assert code == 0
    assert "E02–E17 are outside this nightly gate" in text
    assert "not a single-revision acceptance run" in text
    assert "Full CLI acceptance is not established" in text


def test_unknown_scope_cannot_go_green():
    assert report.render(outcomes(), SHA, SHA, "bogus")[1] == 1


def test_full_scope_still_requires_every_child_to_pass():
    text, code = report.render(outcomes(ec2="failure"), SHA, SHA, "full")
    assert code == 1
    assert "outside this nightly gate" not in text


def test_ec2_snapshot_runs_before_config_and_recovery_uses_its_revision():
    doc, triggers = workflow("eval-cli-uplift.yml")
    steps = doc["jobs"]["evaluate"]["steps"]
    pin = next(i for i, s in enumerate(steps) if s.get("id") == "revision")
    build = next(i for i, s in enumerate(steps) if "--check-ready" in s.get("run", ""))
    assert pin < build
    assert steps[pin]["if"] == "inputs.resolve_revision"
    assert "--ec2" in steps[pin]["run"]
    assert triggers["workflow_call"]["inputs"]["resolve_revision"]["default"] is False
    assert triggers["workflow_call"]["outputs"]["revision"]["value"] == (
        "${{ jobs.evaluate.outputs.revision }}"
    )
    assert doc["jobs"]["recover"]["env"]["CLI_UPLIFT_EVAL_EXPECTED_REVISION"] == (
        "${{ needs.evaluate.outputs.revision || inputs.expected_revision }}"
    )


def test_ec2_snapshot_replaces_only_the_revision_and_publishes_it(
    tmp_path, monkeypatch
):
    for key in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setenv("CLI_UPLIFT_EVAL_EXPECTED_REVISION", "c" * 40)
    monkeypatch.setattr(
        ports, "default_ports", lambda cfg: {"aws": Aws(), "http": Http()}
    )
    assert prepare.main(["--ec2"]) == 0
    assert (
        tmp_path / "GITHUB_ENV"
    ).read_text() == f"CLI_UPLIFT_EVAL_EXPECTED_REVISION={SHA}\n"
    assert (tmp_path / "GITHUB_OUTPUT").read_text() == f"revision={SHA}\n"
    assert "EC2 suite" in (tmp_path / "GITHUB_STEP_SUMMARY").read_text()


def test_fatal_budget_setup_cannot_print_a_passing_cleanup_summary(tmp_path):
    result = run_cleanup(tmp_path, "budget-ratelimit", prior_exit=1)
    assert result.returncode == 1
    assert "aborted before completing" in result.stdout
    assert "SUMMARY failures=1" in result.stdout


def test_budget_seed_supplies_required_json_defaults_to_real_sql(tmp_path):
    # Raw SQL does not apply SQLAlchemy's Python defaults. Execute the actual
    # seeder against NOT NULL constraints, which the old transport stub missed.
    database = tmp_path / "seed.db"
    with sqlite3.connect(database) as db:
        db.executescript("""
        CREATE TABLE organizations (
          id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
          aws_accounts JSON NOT NULL, role_mappings JSON NOT NULL, settings JSON NOT NULL,
          github_installation_ids JSON NOT NULL, cognito_client_ids JSON NOT NULL,
          created_via TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE users (id TEXT PRIMARY KEY, org_id TEXT, team_id TEXT, email TEXT,
          name TEXT, cognito_sub TEXT, created_at TEXT);
        CREATE TABLE tenant_memberships (id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT,
          role TEXT, is_active BOOLEAN, created_at TEXT, UNIQUE(user_id, tenant_id),
          FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(tenant_id) REFERENCES organizations(id));
        CREATE TABLE budget_usage (org_id TEXT);
        CREATE TABLE budget_configs (org_id TEXT);
        CREATE TABLE rate_limit_configs (org_id TEXT);
        """)
    transport = tmp_path / "psql.py"
    transport.write_text("""import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute('PRAGMA foreign_keys=ON')
    db.execute(sys.argv[2].replace('now()', 'CURRENT_TIMESTAMP'))
""")
    source = (ROOT / "platform/evals/budget-ratelimit/run-eval.sh").read_text()
    function = "\n".join(
        re.search(r"^" + name + r"\(\) \{\n.*?^\}", source, re.M | re.S).group()
        for name in (
            "assert_tagged",
            "assert_owned_entity",
            "seed_admin_identity",
            "seed_member_identities",
        )
    )
    setup = r"""
set -euo pipefail
ORG_A=eval-bgt-test-orga ORG_B=eval-bgt-test-orgb EVAL_TAG=eval-bgt-test
TEAM_1=eval-bgt-test-team1 TEAM_2=eval-bgt-test-team2
U1=eval-bgt-test-u1@example.test U2=eval-bgt-test-u2@example.test U3=eval-bgt-test-u3@example.test
X1=eval-bgt-test-x1@example.test A1=eval-bgt-test-a1@example.test
state_get() {
  case "$1" in
    *_SUB) echo "id-${1%_SUB}" ;;
    *_USERNAME) local who="${1%_USERNAME}"; echo "${!who}" ;;
  esac
}
state_set() { :; }
pass() { :; }
die() { echo "$*" >&2; exit 1; }
h_psql() { "$TEST_PYTHON" "$TEST_TRANSPORT" "$TEST_DATABASE" "$2"; }
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            setup + function + "\nseed_admin_identity\nseed_member_identities\n",
        ],
        env={
            **os.environ,
            "TEST_TRANSPORT": str(transport),
            "TEST_DATABASE": str(database),
            "TEST_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(database) as db:
        rows = db.execute(
            "SELECT aws_accounts, role_mappings, settings, created_via FROM organizations"
        ).fetchall()
        assert rows == [("[]", "{}", "{}", "operator")] * 2
        # This is the same canonical-user/tenant lookup required by the budget
        # API; every Cognito actor must resolve in exactly its own tenant.
        for who in ("U1", "U2", "U3", "X1", "A1"):
            org = "eval-bgt-test-orgb" if who == "X1" else "eval-bgt-test-orga"
            assert db.execute(
                "SELECT id FROM users WHERE org_id=? AND cognito_sub=?",
                (org, "id-" + who),
            ).fetchone() == ("id-" + who,)
        assert db.execute(
            "SELECT role, count(*) FROM tenant_memberships GROUP BY role ORDER BY role"
        ).fetchall() == [("member", 4), ("org_admin", 1)]
        # Leave an unrelated real tenant beside the disposable fixtures. Sweep
        # must respect foreign keys AND preserve those unrelated rows.
        db.execute(
            "INSERT INTO organizations SELECT 'real-org', 'Real', aws_accounts, role_mappings, settings, github_installation_ids, cognito_client_ids, created_via, created_at FROM organizations LIMIT 1"
        )
        db.execute(
            "INSERT INTO users (id, org_id, cognito_sub) VALUES ('real-user','real-org','real-sub')"
        )
        db.execute(
            "INSERT INTO tenant_memberships (id,user_id,tenant_id) VALUES ('real-member','real-user','real-org')"
        )
    cleanup = re.search(r"^run_cleanup\(\) \{\n.*?^\}", source, re.M | re.S).group()
    cleanup_setup = r"""
FAILURES=0
WORKDIR="$TEST_WORKDIR"
EVAL_USER_PREFIX=eval-bgt
trace() { :; }
log() { :; }
fail() { echo "$*" >&2; FAILURES=$((FAILURES + 1)); }
delete_seeded_user() { :; }
laptop_pod_delete() { :; }
write_summary() { :; }
"""
    result = subprocess.run(
        ["bash", "-c", setup + cleanup_setup + cleanup + "\ntrue\nrun_cleanup\n"],
        env={
            **os.environ,
            "TEST_DATABASE": str(database),
            "TEST_PYTHON": sys.executable,
            "TEST_TRANSPORT": str(transport),
            "TEST_WORKDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT id FROM organizations").fetchall() == [("real-org",)]
        assert db.execute("SELECT id FROM users").fetchall() == [("real-user",)]
        assert db.execute("SELECT id FROM tenant_memberships").fetchall() == [
            ("real-member",)
        ]


def test_onboarding_uses_the_installer_and_gets_proxy_dependencies(tmp_path):
    source = (ROOT / "platform/evals/cli-onboarding/run-eval.sh").read_text()
    function = re.search(
        r"^install_cli_bundle\(\) \{\n.*?^\}", source, re.M | re.S
    ).group()
    # Exercise the production installer with only its download transport replaced.
    # No product command can reach the network or inherit the operator's HOME.
    fake_bin = tmp_path / "transport"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        + """import os, pathlib, shutil, sys
args=sys.argv[1:]
url=next(a for a in args if a.startswith('https://'))
assert url.startswith('https://gateway.example.test/api/cli/')
shutil.copyfile(pathlib.Path(os.environ['TEST_CLI_FILES']) / url.rsplit('/',1)[1],
                args[args.index('-o')+1])
"""
    )
    curl.chmod(0o755)
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    setup = """
set -euo pipefail
WORKDIR="$HOME"
POD_WORKDIR="$HOME"
POD_HOME="$HOME"
GATEWAY_URL=https://gateway.example.test/api
laptop() { "$@"; }
pass() { echo "$*"; }
fail() { echo "$*" >&2; }
"""
    result = subprocess.run(
        ["bash", "-c", setup + function + "\ninstall_cli_bundle\n"],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
            "TEST_CLI_FILES": str(ROOT / "modules/gateway/cli"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        result.stdout + result.stderr + (home / "install.log").read_text()
    )
    for name in (
        "adp",
        "bg-cognito-auth.sh",
        "bg-gateway-proxy.py",
        "adp_deployments.py",
        "adp_common.py",
    ):
        assert (home / "bin" / name).is_file(), name
    probe = subprocess.run(
        [
            sys.executable,
            str(home / "bin/adp_deployments.py"),
            "proxy-owner",
            str(home / "runtime"),
            "",
            "https://gateway.example.test/api",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr


@pytest.mark.parametrize(
    "kind, value, username, allowed",
    [
        (
            "user",
            "7498f4d8-60e1-70b1-426a-c85d0325eaa9",
            "eval-bgt-current-u1@example.test",
            True,
        ),
        (
            "user",
            "00000000-0000-0000-0000-000000000000",
            "eval-bgt-current-u1@example.test",
            False,
        ),
        (
            "user",
            "7498f4d8-60e1-70b1-426a-c85d0325eaa9",
            "eval-bgt-previous-u1@example.test",
            False,
        ),
        (
            "user",
            "7498f4d8-60e1-70b1-426a-c85d0325eaa9",
            "real-user@example.test",
            False,
        ),
        (
            "user",
            "eval-bgt-current-unseeded",
            "eval-bgt-current-u1@example.test",
            False,
        ),
        ("user", "", "eval-bgt-current-u1@example.test", False),
        ("team", "eval-bgt-current-team", "", True),
        ("team", "production-team", "", False),
    ],
)
def test_budget_mutation_ownership_accepts_only_this_runs_seeded_users(
    kind, value, username, allowed
):
    source = (ROOT / "platform/evals/budget-ratelimit/run-eval.sh").read_text()
    functions = "\n".join(
        re.search(r"^" + name + r"\(\) \{\n.*?^\}", source, re.M | re.S).group()
        for name in ("assert_tagged", "assert_owned_entity")
    )
    setup = r"""
set -euo pipefail
EVAL_TAG=eval-bgt-current
U1=eval-bgt-current-u1@example.test U2=eval-bgt-current-u2@example.test
U3=eval-bgt-current-u3@example.test X1=eval-bgt-current-x1@example.test A1=eval-bgt-current-a1@example.test
die() { echo "$*" >&2; exit 1; }
state_get() {
  case "$1" in
    U1_SUB) echo 7498f4d8-60e1-70b1-426a-c85d0325eaa9 ;;
    U1_USERNAME) echo "$TEST_USERNAME" ;;
    *) echo '' ;;
  esac
}
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            setup + functions + '\nassert_owned_entity "budget entity" "$1" "$2"',
            "_",
            kind,
            value,
        ],
        env={**os.environ, "TEST_USERNAME": username},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr
    if not allowed:
        assert "REFUSING" in result.stderr


def test_both_budget_writers_check_entity_ownership_and_org_tag_before_posting():
    source = (ROOT / "platform/evals/budget-ratelimit/run-eval.sh").read_text()
    for name in ("set_budget", "set_ratelimit"):
        function = re.search(
            r"^" + name + r"\(\) \{\n.*?^\}", source, re.M | re.S
        ).group()
        assert function.index("assert_owned_entity") < function.index("http_post_json")
        assert function.index("assert_tagged") < function.index("http_post_json")
