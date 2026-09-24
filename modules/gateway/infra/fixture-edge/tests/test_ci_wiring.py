"""Gate for the fixture edge's own CI wiring (Issue #5836, blocking area 5).

The review's finding was "CI does not run these tests". The fix is a workflow,
but a workflow is exactly the kind of artifact that rots silently: the 25
Terraform tests and 50 pytest tests it invokes all pass locally, and they also
all pass when NOTHING CALLS THEM. Every other gate in this component stays green
in that state. So the wiring needs its own gate, for the same reason
.github/scripts/tests/test_nightly_triage_wiring.py exists.

The specific failure this component started from is worth naming, because a
green check was part of it rather than absent:

    `gateway-infra-plan.yml` triggers on `modules/gateway/infra/**`, which
    matches `modules/gateway/infra/fixture-edge/**`. A fixture-edge-only PR
    therefore triggered a credentialed job that plans the ORDINARY gateway,
    posted a green check, and executed none of this root's tests. fixture-edge
    is not referenced as a module by the gateway root, so that plan never
    evaluated a line of it.

That is worse than no CI: "no CI" is visible, a green check about a different
component is not. The gates below pin the properties that make the new workflow
real evidence.

Deliberately NOT asserted here: that the tests themselves pass. They are run by
the workflow and by every other file in this directory; asserting it again here
would be a second place to update and would not catch anything.
"""

import re
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
COMPONENT = HERE.parent
REPO_ROOT = COMPONENT.parents[3]

WORKFLOW = REPO_ROOT / ".github/workflows/fixture-edge-ci.yml"
COMPONENT_GLOB = "modules/gateway/infra/fixture-edge/**"
JOB = "fixture-edge"


@pytest.fixture(scope="module")
def wf():
    assert WORKFLOW.is_file(), (
        f"{WORKFLOW} is missing. Without it a fixture-edge-only PR runs no test "
        "of this component — see this module's docstring."
    )
    loaded = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML resolves the bare `on:` key to the boolean True (YAML 1.1). Handle
    # both so this gate does not depend on which parser sees the file.
    loaded["on"] = loaded.get("on", loaded.get(True))
    assert loaded["on"] is not None, "workflow has no trigger block"
    return loaded


@pytest.fixture(scope="module")
def job(wf):
    assert JOB in wf["jobs"], f"expected a '{JOB}' job, found {list(wf['jobs'])}"
    return wf["jobs"][JOB]


@pytest.fixture(scope="module")
def steps(job):
    return job["steps"]


def _run_text(steps):
    return "\n".join(s.get("run", "") for s in steps)


# ---------------------------------------------------------------------------
# REACHABILITY — the finding itself
# ---------------------------------------------------------------------------
def test_a_change_to_this_component_triggers_the_workflow(wf):
    """The whole directory, not an enumeration of files.

    An enumeration is how this kind of trigger goes stale: someone adds
    variables-v2.tf or a second .tftest.hcl and it runs no CI at all.
    """
    paths = wf["on"]["pull_request"]["paths"]
    assert COMPONENT_GLOB in paths, (
        f"{COMPONENT_GLOB} must be a trigger path, else an edit to this "
        f"component runs none of its tests. Found: {paths}"
    )


def test_the_workflow_triggers_on_itself(wf):
    paths = wf["on"]["pull_request"]["paths"]
    assert ".github/workflows/fixture-edge-ci.yml" in paths, (
        "A change to the workflow must run the workflow, or a broken edit to it "
        "is only discovered on some later unrelated PR."
    )


def test_the_3968_ownership_library_is_a_trigger_path(wf):
    """Lockstep reachability across the coordination seam.

    Both scripts record their Kubernetes objects via #3968's
    `lib/ownership.py record-k8s --uid`, and the pytest suites assert against a
    FAKE of that interface. A fake cannot notice that the real CLI's flags
    changed. Without this path, such a change runs these tests in no PR and the
    break surfaces at operator time, mid-run.
    """
    paths = wf["on"]["pull_request"]["paths"]
    assert "platform/scripts/operator/wave2/lib/ownership.py" in paths, (
        "The #3968 ownership library must be a trigger path — the fakes in "
        "tests/ would keep passing while the real invocation broke."
    )


def test_every_test_file_in_this_component_is_executed(steps):
    """Catches the 'added a suite, never wired it' regression.

    Derived from the filesystem rather than a hardcoded list, so a new
    test_*.py cannot be added without either being run or failing this gate.
    """
    run = _run_text(steps)
    discovered = sorted(p.name for p in HERE.glob("test_*.py"))
    assert discovered, "no pytest files discovered — this gate would be vacuous"
    missing = [n for n in discovered if n not in run]
    assert not missing, (
        f"these test files exist but no workflow step runs them: {missing}. "
        "A suite nobody calls passes for free."
    )


def test_terraform_test_is_unfiltered(steps):
    """`-filter` is how a new run block silently stops being executed.

    webhook-ingress-ci.yml uses -filter deliberately for a partial suite; here
    the whole file should run, so adding a run block must not require a
    workflow edit.
    """
    tf_test = [
        s for s in steps if re.search(r"\bterraform test\b", s.get("run", ""))
    ]
    assert tf_test, "no step runs `terraform test`"
    for s in tf_test:
        assert "-filter" not in s["run"], (
            "terraform test must not be filtered: a run block added to "
            "fixture_edge_test.tftest.hcl would otherwise never execute."
        )


def test_fmt_validate_and_test_all_run(steps):
    run = _run_text(steps)
    for needle in ("terraform fmt -check", "terraform validate", "terraform test"):
        assert needle in run, f"missing `{needle}` — see RUNBOOK pre-submit checks"


# ---------------------------------------------------------------------------
# PREREQUISITES — a test that cannot run is not a test that passed
# ---------------------------------------------------------------------------
# Root's finding, from an actual CI run: the job reported 5 failed / 120 passed,
# and all five were the signed-probe cases returning HTTP 000 — an UNSIGNED
# request, which is the very defect they exist to detect, caused here by boto3
# being absent rather than by the script. The same run's real-kubectl tests could
# equally have SKIPPED silently, which would have been worse: a skip is reported
# as success.
#
# So the runtime prerequisites of the two checks that reach outside the fakes are
# gated here.
def test_boto3_is_installed_for_the_signed_probe(steps):
    """The wrong-role control signs through botocore's own provider chain.

    That is not stylistic: the previous revision read keys with `aws configure get`,
    which returns nothing under assumed-role/SSO, so the probe could never sign and
    a 403 it recorded proved nothing. The replacement imports boto3 in the process
    under test, so an environment without boto3 turns the control's own test into a
    dependency failure that LOOKS like the defect.
    """
    installs = [s.get("run", "") for s in steps if "pip install" in s.get("run", "")]
    assert installs, "no dependency install step found"
    assert any(re.search(r"\bboto3\b", r) for r in installs), (
        "boto3 is not installed. The signed wrong-role probe then cannot sign, and "
        "its tests fail with HTTP 000 — indistinguishable from the unsigned-probe "
        "defect they are meant to catch."
    )


def test_the_real_kubectl_is_installed(steps):
    """Two tests hand the ACTUAL command line to the REAL kubectl (`--local`, so no
    cluster and no credentials). They are the only checks here not made against a
    double, and they exist because the fake accepted a flag kubectl does not have."""
    run = _run_text(steps)
    assert re.search(r"kubectl", run), (
        "no step installs kubectl, so the only real-CLI regression in this suite "
        "cannot execute in CI — the workflow comments claiming it does would be false."
    )
    assert "dl.k8s.io" in run or "setup-kubectl" in run, (
        "kubectl is mentioned but not installed from a release or an action"
    )


def test_a_missing_kubectl_fails_the_job_instead_of_skipping_it(steps):
    """THE SKIP-IS-NOT-A-PASS GATE.

    If the install step above ever breaks or is removed, the two real-CLI tests
    would `pytest.skip` and the job would stay green with the interface unchecked.
    The pytest suite escalates that skip to a failure when
    FIXTURE_EDGE_REQUIRE_REAL_KUBECTL=1, so this asserts the step that runs it
    declares that variable.
    """
    running = [s for s in steps
               if "test_fixture_lifecycle.py" in s.get("run", "")]
    assert running, "no step runs the lifecycle suite"
    for s in running:
        env = s.get("env") or {}
        assert str(env.get("FIXTURE_EDGE_REQUIRE_REAL_KUBECTL")) == "1", (
            f"step '{s.get('name', '?')}' runs the lifecycle suite without "
            "FIXTURE_EDGE_REQUIRE_REAL_KUBECTL=1, so an absent kubectl would skip "
            "the real-CLI regression and still report a green check."
        )


# ---------------------------------------------------------------------------
# NO PRODUCTION CREDENTIALS — #5836 forbids live mutation from CI
# ---------------------------------------------------------------------------
# arc-runner-org carries ambient IRSA credentials. NOT REQUESTING credentials is
# not the same as NOT HAVING them, so these gates check what is actively
# enforced, not merely what is absent.
def test_no_oidc_token_permission(wf, job):
    for scope, node in (("workflow", wf), (JOB, job)):
        perms = node.get("permissions") or {}
        if isinstance(perms, dict):
            assert perms.get("id-token") != "write", (
                f"{scope} requests id-token: write, which permits assuming a "
                "deployment role. This component's CI must not reach AWS."
            )
    assert (job.get("permissions") or {}).get("contents") == "read", (
        "the job should declare contents: read explicitly rather than inherit "
        "a broader default"
    )


def test_no_credential_configuration_step(steps):
    for s in steps:
        uses = s.get("uses", "")
        assert "configure-aws-credentials" not in uses, (
            f"step '{s.get('name', uses)}' configures AWS credentials. The "
            "mocked suites need none; having them is what makes an accidental "
            "live call possible."
        )


def test_no_step_can_write_state_or_mutate(steps):
    """apply/destroy/plan are all unacceptable here, for different reasons.

    apply and destroy mutate. `plan` is included because a plan needs real
    credentials and a real backend — its presence would mean this job had both.
    """
    for s in steps:
        run = s.get("run", "")
        for verb in ("terraform apply", "terraform destroy", "terraform plan"):
            assert verb not in run, (
                f"step '{s.get('name', '?')}' runs `{verb}`. Only "
                "fmt/validate/test are permitted in this component's CI."
            )


def test_every_terraform_init_disables_the_backend(steps):
    """This is the structural guard, not a stylistic preference.

    With `backend "s3"` declared, an init WITHOUT -backend=false would try to
    configure real S3 state. With it, `terraform plan` is not merely skipped but
    impossible: it exits non-zero with "Backend initialization required"
    (reproduced on Terraform 1.14.6).
    """
    inits = [s for s in steps if re.search(r"terraform init", s.get("run", ""))]
    assert inits, "no `terraform init` step found"
    for s in inits:
        assert "-backend=false" in s["run"], (
            f"step '{s.get('name', '?')}' inits without -backend=false, which "
            "would configure the real S3 backend."
        )


def test_aws_env_points_away_from_any_real_account(wf):
    """Defence in depth: make a leaked SDK call fail loudly, not silently work.

    Asserted as 'not a plausible credential' rather than an exact string, so the
    values can be reworded without turning this gate off.
    """
    env = wf.get("env") or {}
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert key in env, f"{key} should be pinned to an unusable value"
        val = str(env[key])
        assert "${{" not in val, (
            f"{key} must not interpolate a secret — that would give this job "
            "real credentials."
        )
        assert not re.fullmatch(r"AKIA[0-9A-Z]{16}", val), f"{key} looks real"
    assert str(env.get("AWS_EC2_METADATA_DISABLED")).lower() == "true", (
        "IMDS must be disabled: on arc-runner-org it is the path by which an "
        "SDK call picks up the cluster's ambient role."
    )


# ---------------------------------------------------------------------------
# HONESTY OF THE GREEN CHECK
# ---------------------------------------------------------------------------
def test_the_workflow_states_it_is_not_live_acceptance(wf):
    """A green check here must not be readable as live acceptance.

    Root's constraint: "Currenttests/CIgreen are not liveacceptance and do not
    supersede these findings." The workflow is the artifact a reader sees first,
    so the disclaimer belongs in it.
    """
    text = WORKFLOW.read_text().lower()
    assert "not live acceptance" in text, (
        "the workflow must say a green check is not live acceptance"
    )
    assert "mock" in text, "the workflow must say the providers are mocked"


def test_the_job_name_identifies_what_was_checked(wf, job):
    """The original defect was a green check whose NAME described another thing.

    "Gateway Infra Plan" passing on a fixture-edge PR was true and irrelevant.
    This job's name must say fixture.
    """
    assert "fixture" in job.get("name", "").lower(), (
        f"job name {job.get('name')!r} should identify the component, so a "
        "green check cannot be misread as covering something else"
    )
    assert "fixture" in wf["name"].lower()
