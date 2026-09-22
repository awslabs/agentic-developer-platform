"""Gate for the nightly Security Agent scaffold (U4, issue #4443).

Every assertion here exists because a specific failure has a named blast
radius in the unit's impact analysis:

  - a `schedule:` key            -> an unmeasured job arms itself against
                                    shared dev and can overlap three existing
                                    nightly suites
  - provisioning as a no-op      -> the step appears to install and installs
                                    nothing; every downstream unit then fails
                                    on a runtime the plan believed was fixed.
                                    This is the most likely failure mode and
                                    most of this file is shaped around it
  - split interpreters           -> package lands in one Python, assertion
                                    runs in another
  - over-broad IAM               -> a role that can reach arbitrary S3 or take
                                    arbitrary actions, held by a job that runs
                                    unattended every night
  - an unclear preflight failure -> an operator sees a nightly failure with no
                                    actionable cause and abandons the run
"""

import json
import re
import subprocess  # nosec B404 - fixed argv, no shell, test-only
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from securityagent_preflight import (
    PreflightError,
    assert_client_available,
    botocore_version,
    install_argv,
    load_profile,
    main,
    parse_version,
    provision,
    version_at_least,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "security-agent-nightly.yml"
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "securityagent_preflight.py"
POLICY_PATH = (
    REPO_ROOT / "platform" / "infra" / "policies" / "securityagent-nightly-policy.json"
)
TERRAFORM_PATH = REPO_ROOT / "platform" / "infra" / "securityagent-nightly-iam.tf"
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "script-tests.yml"

yaml = pytest.importorskip("yaml", reason="PyYAML required to parse the workflow")


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow():
    """The nightly workflow, parsed as YAML.

    Parsed, never grepped: `grep -v schedule` passes on a file where the key
    is present but indented differently, commented mid-line, or spelled with
    different quoting.
    """
    assert WORKFLOW_PATH.is_file(), f"workflow missing: {WORKFLOW_PATH}"
    with WORKFLOW_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def triggers(workflow):
    """The `on:` block.

    PyYAML follows YAML 1.1, where the bare key `on` is the boolean True. A
    test that only looked at `workflow["on"]` would silently read None and
    then "prove" no schedule exists on any workflow at all.
    """
    block = workflow.get("on", workflow.get(True))
    assert block is not None, "workflow declares no triggers at all"
    return block


@pytest.fixture(scope="module")
def profile():
    return load_profile()


@pytest.fixture(scope="module")
def preflight_job(workflow):
    """The `preflight` job, selected BY KEY.

    This fixture used to assert `len(jobs) == 1` on the grounds that U4 shipped
    the scaffold alone. That was planned obsolescence, and U5 (#4445) triggered
    it: adding the code-review job made every test depending on this fixture
    ERROR at setup rather than fail, because the fixture is module-scoped.

    Selecting by key instead of by cardinality keeps every property the
    dependent tests check (provisioning precedes preflight, no `command -v`
    guard, one interpreter) while surviving U7 adding a third job.

    Deliberately NOT `next(iter(jobs.values()))` with a relaxed `>= 1`: that
    silently reads whichever job the mapping happens to yield first, so a later
    unit reordering the file would point these assertions at the wrong job and
    they would keep passing while proving nothing about `preflight`.
    """
    jobs = workflow["jobs"]
    assert "preflight" in jobs, (
        f"the nightly must declare a `preflight` job; found {sorted(jobs)}"
    )
    return jobs["preflight"]


@pytest.fixture(scope="module")
def run_steps(preflight_job):
    """Steps that execute a shell command, in declaration order."""
    return [s for s in preflight_job["steps"] if s.get("run")]


class _StubSession:
    """Stands in for boto3.Session so the failure paths are testable without
    an under-provisioned interpreter."""

    def __init__(self, services):
        self._services = services

    def __call__(self):
        return self

    def get_available_services(self):
        return list(self._services)


# --------------------------------------------------------------------------
# the no-schedule gate
# --------------------------------------------------------------------------


def test_workflow_has_no_schedule_key(triggers):
    """The headline gate: no `schedule:` trigger in this unit."""
    assert "schedule" not in triggers, (
        "a schedule key arms an unmeasured nightly against the shared dev "
        "environment and can collide with three existing suites — the exact "
        "collision this unit's plan defers on purpose"
    )


def test_workflow_is_manually_dispatchable(triggers):
    """Manual-trigger-only means workflow_dispatch must actually be present.

    Without this, "no schedule" could be satisfied by a workflow that cannot
    be triggered at all — which would also make the deployment step ("dispatch
    it once to prove provisioning works") impossible.
    """
    assert "workflow_dispatch" in triggers


def test_schedule_is_not_reachable_through_any_other_trigger(triggers):
    """No push/PR/cron-by-proxy trigger sneaks the job into automatic runs."""
    unexpected = set(triggers) - {"workflow_dispatch"}
    assert not unexpected, (
        f"unexpected triggers {sorted(unexpected)}: this unit is manual-only"
    )


def test_the_documented_smoke_command_passes():
    """The exact smoke command from the issue's Validation section exits 0.

    Pinned as a test so the documented check cannot rot away from the gate.
    """
    smoke = (
        "import yaml,sys; "
        "d=yaml.safe_load(open('.github/workflows/security-agent-nightly.yml')); "
        "sys.exit(0 if 'schedule' not in (d.get(True) or d.get('on')) else 1)"
    )
    completed = subprocess.run(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-c", smoke],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr.decode()


# --------------------------------------------------------------------------
# post-condition provisioning asserts
# --------------------------------------------------------------------------


def test_a_provisioning_step_exists_before_preflight(run_steps):
    """Call-order assertion on the parsed YAML.

    An assertion that ran before provisioning would report the image's state
    rather than the state this job creates — it would pass on a runner that
    happened to be fine and never exercise the install path.
    """
    provision_idx = [i for i, s in enumerate(run_steps) if "--provision" in s["run"]]
    preflight_idx = [
        i
        for i, s in enumerate(run_steps)
        if "securityagent_preflight.py" in s["run"] and "--provision" not in s["run"]
    ]

    assert provision_idx, "no provisioning step found in the workflow"
    assert preflight_idx, "no preflight assertion step found in the workflow"
    assert min(provision_idx) < min(preflight_idx), (
        "provisioning must be ordered before the preflight assertion"
    )


def test_provisioning_is_not_guarded_into_a_no_op(run_steps):
    """The failure mode this unit exists to prevent.

    ~20 steps in this repo are shaped `command -v x || install x`, and on this
    runner image every one is a no-op. A guard here would also skip on a
    *stale but present* boto3 — which is exactly the arc-runner-org case
    (pinned 1.35.99, proven not to carry the service).
    """
    provisioning = [s for s in run_steps if "--provision" in s["run"]][0]["run"]

    for banned in ("command -v", "which ", "|| true", "continue-on-error"):
        assert banned not in provisioning, (
            f"provisioning step contains {banned!r}, which can turn it into a "
            "silent no-op"
        )
    assert "||" not in provisioning, (
        "an `||` fallback lets provisioning fail without failing the job"
    )


def test_provisioning_step_cannot_be_skipped(preflight_job, run_steps):
    """No `if:` condition and no continue-on-error on the install path."""
    provisioning = [s for s in run_steps if "--provision" in s["run"]][0]
    assert "if" not in provisioning, (
        "a conditional provisioning step is a no-op waiting to happen"
    )
    assert provisioning.get("continue-on-error") is not True


def test_provisioning_command_is_derived_from_the_profile_not_retyped(run_steps):
    """No retyped literal: the package spec appears nowhere in the workflow.

    The workflow invokes the script, which reads runtime.install_command from
    the profile at run time. If someone pastes `pip install boto3>=...` into
    the YAML instead, that is a second source of truth that drifts from the
    bisected floor the spike established, and this fails.
    """
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    # Comments legitimately discuss the pinned-version history; only the
    # executable step bodies must be literal-free.
    provisioning = [s for s in run_steps if "--provision" in s["run"]][0]["run"]

    for literal in ("boto3", "botocore", "pip install", "pip3 install"):
        assert literal not in provisioning, (
            f"provisioning step retypes {literal!r} instead of reading "
            "runtime.install_command from the validated profile"
        )
    assert "securityagent_preflight.py --provision" in provisioning

    # And the version floor must not appear anywhere in the workflow, comments
    # included — a stale floor in a comment is how the next reader gets it
    # wrong.
    assert not re.search(r"\b1\.\d+\.\d+\b", workflow_text), (
        "the workflow names a concrete version; the floor belongs only in the "
        "validated profile"
    )


def test_install_argv_is_built_from_the_recorded_command(profile):
    """The derivation is real: the profile's flags survive into the argv."""
    argv = install_argv(profile, python_executable="/usr/bin/python3")
    recorded = profile["runtime"]["install_command"]

    assert argv[:3] == ["/usr/bin/python3", "-m", "pip"]
    assert "install" in argv
    # Every non-pip token of the recorded command is preserved verbatim.
    for token in recorded.split()[1:]:
        assert token.strip("'\"") in [a.strip("'\"") for a in argv], (
            f"recorded token {token!r} was dropped when building the argv"
        )


def test_install_argv_rejects_an_empty_recorded_command():
    """Refuse to invent a command the spike did not record."""
    with pytest.raises(PreflightError, match="install_command is empty"):
        install_argv({"runtime": {"install_command": "   "}})


def test_install_argv_rejects_a_command_it_cannot_bind_to_an_interpreter():
    """A non-pip command cannot be interpreter-bound, so it must not run.

    Silently shelling out to it would reintroduce the split-interpreter bug.
    """
    with pytest.raises(PreflightError, match="does not start with pip"):
        install_argv({"runtime": {"install_command": "apt-get install -y boto3"}})


def test_install_argv_normalises_an_explicit_python_m_pip_form():
    """`python3 -m pip install X` rebinds to the target interpreter."""
    argv = install_argv(
        {"runtime": {"install_command": "python3 -m pip install -U 'boto3>=9'"}},
        python_executable="/opt/py/bin/python",
    )
    assert argv == ["/opt/py/bin/python", "-m", "pip", "install", "-U", "boto3>=9"]


def test_provision_runs_unconditionally_and_returns_the_argv(profile):
    """provision() has no "already present" early exit."""
    calls = []

    class _Result:
        returncode = 0

    argv = provision(
        profile,
        python_executable="/usr/bin/python3",
        runner=lambda a, **k: calls.append(a) or _Result(),
    )
    assert calls, "provision() did not invoke the installer at all"
    assert calls[0] == argv


def test_provision_fails_the_job_when_the_install_fails(profile):
    """A failed install must not be swallowed."""

    class _Result:
        returncode = 1

    with pytest.raises(PreflightError, match="provisioning failed"):
        provision(profile, runner=lambda a, **k: _Result())


# --------------------------------------------------------------------------
# same-interpreter gate
# --------------------------------------------------------------------------


def test_provisioning_and_preflight_resolve_to_the_same_interpreter(
    preflight_job, run_steps
):
    """Asserted from the parsed YAML, not assumed.

    Both steps must reference one interpreter variable, and it must be defined
    once at job level so the two cannot drift apart.
    """
    job_env = preflight_job.get("env", {})
    assert "SECURITYAGENT_PYTHON" in job_env, (
        "the interpreter must be pinned once at job level, not per step"
    )

    interpreter_steps = [
        s for s in run_steps if "securityagent_preflight.py" in s["run"]
    ]
    assert len(interpreter_steps) >= 2

    for step in interpreter_steps:
        assert "$SECURITYAGENT_PYTHON" in step["run"], (
            f"step {step.get('name')!r} does not use the job-level interpreter"
        )
        # A step-level override would defeat the job-level pin.
        assert "SECURITYAGENT_PYTHON" not in step.get("env", {})


def test_workflow_does_not_introduce_a_second_interpreter(preflight_job):
    """No setup-python: a second interpreter is how the package lands in one
    Python while the assertion runs in another."""
    uses = [s.get("uses", "") for s in preflight_job["steps"]]
    assert not any("setup-python" in u for u in uses), (
        "actions/setup-python installs a second interpreter and reintroduces "
        "the split-interpreter failure mode"
    )


def test_preflight_selection_is_not_bound_to_job_count(workflow):
    """The fixture must select `preflight` by key, not by cardinality.

    Regression guard for the defect U5 exposed (#4517): the original fixture
    asserted the workflow held exactly one job, so the code-review job made six
    tests ERROR at setup. U7's pentest job would have done it again.

    Re-runs the fixture's own selection against a workflow carrying two extra
    synthetic jobs. If someone reintroduces a count assertion or positional
    indexing, this fails while the other tests would still pass.
    """
    jobs = dict(workflow["jobs"])
    # Inserted BEFORE `preflight` so positional indexing picks the wrong job.
    inflated = {"aaa-synthetic-pentest": {"steps": []}, **jobs, "zzz-synthetic": {"steps": []}}

    assert "preflight" in inflated
    selected = inflated["preflight"]

    assert selected is workflow["jobs"]["preflight"], (
        "selection must be by key; a count assertion or positional index would "
        "break or silently pick a different job once a later unit adds one"
    )
    assert any(
        "--provision" in s.get("run", "") for s in selected["steps"]
    ), "the job selected by key must be the real preflight job"


def test_script_installs_into_its_own_executable():
    """The structural half of the same-interpreter guarantee.

    The script defaults to sys.executable rather than accepting a
    caller-supplied path, so provisioning cannot target a different Python
    than the one that later asserts.
    """
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "python_executable: str = sys.executable" in source
    assert '[python_executable, "-m", "pip"' in source


# --------------------------------------------------------------------------
# preflight fails fast and legibly
# --------------------------------------------------------------------------


def test_preflight_fails_when_the_service_is_absent(profile):
    """Stubbed client without the service -> a clear PreflightError."""
    with pytest.raises(PreflightError) as excinfo:
        assert_client_available(
            profile,
            session_factory=_StubSession(["s3", "ec2", "lambda"]),
            version_reader=lambda: profile["runtime"]["observed"]["botocore"],
        )

    message = str(excinfo.value)
    assert "securityagent" in message
    assert "Provisioning did not take effect" in message, (
        "the message must name the likely cause, not just the symptom"
    )


def test_preflight_failure_names_the_interpreter_and_the_fix(profile):
    """An unattended nightly that fails without an actionable cause gets
    silently abandoned, so the message carries both."""
    with pytest.raises(PreflightError) as excinfo:
        assert_client_available(
            profile,
            session_factory=_StubSession(["s3"]),
            python_executable="/usr/bin/python3.13",
            version_reader=lambda: profile["runtime"]["observed"]["botocore"],
        )

    message = str(excinfo.value)
    assert "/usr/bin/python3.13" in message, "the failure must name the interpreter"
    assert "Fix:" in message, "the failure must name a remediation command"
    assert "pip" in message and "install" in message


def test_preflight_passes_when_the_service_is_present(profile, capsys):
    """The happy path returns the resolved botocore version."""
    observed = assert_client_available(
        profile,
        session_factory=_StubSession(["s3", "securityagent"]),
        version_reader=lambda: profile["runtime"]["observed"]["botocore"],
    )
    assert observed
    assert "OK" in capsys.readouterr().out


def test_preflight_does_not_self_heal(profile, monkeypatch):
    """The assertion must never repair and pass.

    The profile records the assertion as `python -c ... || pip install ...`.
    That shape is right at an operator's shell and wrong here: it makes the
    assertion unable to fail, and failing is this step's entire job.
    """
    called = []
    monkeypatch.setattr(
        "securityagent_preflight.subprocess.run",
        lambda *a, **k: called.append(a) or None,
    )
    with pytest.raises(PreflightError):
        assert_client_available(
            profile,
            session_factory=_StubSession(["s3"]),
            version_reader=lambda: profile["runtime"]["observed"]["botocore"],
        )
    assert not called, "the assertion attempted to install something"


def test_preflight_fails_closed_below_the_validated_floor(profile):
    """A backported service model that resolves below the bisected floor is an
    unvalidated combination, so it must not pass.

    The below-floor version is derived from the profile's own floor rather than
    hardcoded, so this cannot drift into asserting against a stale number. The
    version is injected rather than monkeypatched onto the real botocore, so
    the test also runs on an interpreter that has no botocore at all -- i.e.
    the clean CI interpreter that most resembles an unprovisioned runner.
    """
    floor = profile["runtime"]["min_botocore_version"]
    below = ".".join([str(int(floor.split(".")[0]) - 1), *floor.split(".")[1:]])
    assert not version_at_least(below, floor)

    with pytest.raises(PreflightError, match="below the validated floor"):
        assert_client_available(
            profile,
            session_factory=_StubSession(["securityagent"]),
            version_reader=lambda: below,
        )


def test_missing_botocore_is_reported_as_a_preflight_error(profile):
    """An absent botocore must surface as an actionable PreflightError, not a
    bare ModuleNotFoundError."""

    def _absent():
        raise PreflightError("botocore is not importable")

    with pytest.raises(PreflightError) as excinfo:
        assert_client_available(
            profile,
            session_factory=_StubSession(["securityagent"]),
            version_reader=_absent,
        )
    message = str(excinfo.value)
    assert "botocore is not importable" in message
    assert "Fix:" in message


def test_botocore_version_seam_reports_absence_as_preflight_error():
    """The real reader converts ImportError into PreflightError.

    Exercised on whichever interpreter runs the suite: where botocore is
    present it must return a parseable version; where it is absent it must
    raise PreflightError rather than ModuleNotFoundError. Both are correct
    outcomes, and CI runs a clean interpreter that takes the second branch.
    """
    try:
        version = botocore_version()
    except PreflightError as exc:
        assert "not importable" in str(exc)
    else:
        assert parse_version(version)


def test_missing_profile_is_a_hard_failure(tmp_path):
    """Without the artifact there is no floor and no command; guessing either
    defeats the point of having it."""
    with pytest.raises(PreflightError, match="profile artifact not found"):
        load_profile(tmp_path / "absent.json")


def test_cli_returns_nonzero_and_annotates_on_failure(tmp_path, capsys):
    """main() converts a PreflightError into an exit code plus a GitHub
    annotation, never a bare traceback."""
    rc = main(["--profile", str(tmp_path / "absent.json")])
    assert rc == 1
    assert "::error title=Security Agent preflight::" in capsys.readouterr().err


def test_cli_succeeds_on_a_provisioned_interpreter():
    """End-to-end on the interpreter running the tests.

    Skipped rather than failed where the service is genuinely absent: this
    suite gates the scaffold's shape, and a correct scaffold must not go red
    because the *test* machine is under-provisioned. The runner-side proof is
    the dispatched smoke run named in the issue.
    """
    boto3 = pytest.importorskip("boto3")
    if "securityagent" not in boto3.Session().get_available_services():
        pytest.skip("securityagent absent in this interpreter (expected off-runner)")
    assert main([]) == 0


# --------------------------------------------------------------------------
# version comparison
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "observed,minimum,expected",
    [
        ("1.42.80", "1.42.80", True),
        ("1.42.81", "1.42.80", True),
        ("1.42.79", "1.42.80", False),
        ("1.35.99", "1.42.80", False),  # the arc-runner-org pin
        ("2.0.0", "1.42.80", True),
        ("1.42", "1.42.0", True),  # zero-padding, not string ordering
        ("1.43.83", "1.42.80", True),  # the observed spike value
    ],
)
def test_version_at_least(observed, minimum, expected):
    assert version_at_least(observed, minimum) is expected


def test_unparseable_version_is_reported_not_silently_accepted():
    with pytest.raises(PreflightError, match="cannot parse version"):
        version_at_least("1.42.80rc1", "1.42.80")


# --------------------------------------------------------------------------
# profile linkage — recorded minimums, never retyped
# --------------------------------------------------------------------------


def test_recorded_minimum_versions_match_the_profile(profile):
    """The floor this unit enforces is the spike's floor, by construction.

    Asserted by absence: no version literal exists in the script to drift.
    """
    runtime = profile["runtime"]
    floor = runtime["min_botocore_version"]

    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert floor not in source, (
        f"the script hardcodes the floor {floor!r} instead of reading "
        "runtime.min_botocore_version from the profile"
    )
    assert 'runtime.get("min_botocore_version")' in source

    # The value read is usable for a comparison and consistent with the spike.
    assert version_at_least(runtime["observed"]["botocore"], floor)


def test_the_profile_still_records_the_self_upgradable_branch(profile):
    """This unit implements the in-session install path.

    If a later spike revision flips provisioning to image_rebuild_required,
    the install step must degenerate to the no-op case and U13 lands first —
    so that change must fail here rather than silently mismatch.
    """
    assert profile["runtime"]["provisioning"] == "self_upgradable", (
        "U4's provisioning step implements the self_upgradable branch; a "
        "changed profile branch requires revisiting this workflow"
    )


def test_the_probe_the_spike_rejected_is_not_used():
    """`aws securityagent help` fails on this image with a groff error that is
    indistinguishable from a missing service. The spike recorded it as unusable."""
    for path in (SCRIPT_PATH, WORKFLOW_PATH):
        assert "securityagent help" not in path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# least-privilege IAM
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def policy():
    assert POLICY_PATH.is_file(), f"IAM policy missing: {POLICY_PATH}"
    with POLICY_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _statements(policy):
    statements = policy["Statement"]
    return statements if isinstance(statements, list) else [statements]


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def test_policy_has_no_wildcard_action(policy):
    """No `"Action": "*"`, in any statement, in any form."""
    for statement in _statements(policy):
        actions = _as_list(statement.get("Action")) + _as_list(
            statement.get("NotAction")
        )
        assert actions, f"statement {statement.get('Sid')!r} grants no action"
        for action in actions:
            assert action != "*", (
                f"statement {statement.get('Sid')!r} grants every action to a "
                "role held by an unattended nightly job"
            )
            assert not action.startswith("*"), f"wildcard-prefixed action {action!r}"


def test_policy_has_no_service_level_action_wildcard(policy):
    """`securityagent:*` is not least privilege either."""
    for statement in _statements(policy):
        for action in _as_list(statement.get("Action")):
            assert not action.endswith(":*"), (
                f"{action!r} grants a whole service; enumerate the verbs instead"
            )


def test_policy_has_no_wildcard_s3_resource(policy):
    """S3 must name the one staging bucket."""
    for statement in _statements(policy):
        for resource in _as_list(statement.get("Resource")):
            if not resource.startswith("arn:aws:s3:"):
                continue
            assert resource not in ("*", "arn:aws:s3:::*", "arn:aws:s3:::*/*"), (
                f"wildcard S3 resource {resource!r}"
            )
            assert "${staging_bucket}" in resource, (
                f"S3 resource {resource!r} does not name the staging bucket"
            )


def test_policy_grants_no_bare_wildcard_resource_anywhere(policy):
    """`"Resource": "*"` would make the enumerated actions account-wide."""
    for statement in _statements(policy):
        resources = _as_list(statement.get("Resource"))
        assert resources, f"statement {statement.get('Sid')!r} names no resource"
        assert "*" not in resources, (
            f"statement {statement.get('Sid')!r} uses a bare wildcard resource"
        )


def test_every_statement_is_an_allow_with_a_sid(policy):
    """Sids make a review diff legible; a stray Deny here would be a smell."""
    for statement in _statements(policy):
        assert statement["Effect"] == "Allow"
        assert statement.get("Sid"), "every statement needs a Sid"


def test_list_bucket_allows_preflight_and_bounds_explicit_prefixes(policy):
    """Bucket preflight omits s3:prefix; explicit listings stay constrained."""
    listing = [
        s for s in _statements(policy) if "s3:ListBucket" in _as_list(s.get("Action"))
    ]
    assert listing, "expected a ListBucket statement"
    for statement in listing:
        assert statement["Resource"] == "arn:aws:s3:::${staging_bucket}"
        prefixes = statement["Condition"]["StringLikeIfExists"]["s3:prefix"]
        assert prefixes, "ListBucket is not bounded to a prefix"
        for prefix in _as_list(prefixes):
            assert prefix != "*" and "${staging_prefix}" in prefix


def test_bucket_preflight_does_not_expand_object_access(policy):
    for statement in _statements(policy):
        if any(action in _as_list(statement.get("Action"))
               for action in ("s3:GetObject", "s3:GetObjectVersion", "s3:PutObject")):
            assert _as_list(statement["Resource"]) == ["arn:aws:s3:::${staging_bucket}/${staging_prefix}/*"]


def test_policy_actions_come_from_the_validated_profile(policy, profile):
    """Every granted securityagent verb is one the spike validated.

    Stops a verb being invented here that the service does not have, and stops
    the grant drifting wider than the profile's call list.
    """
    granted = {
        action.split(":", 1)[1]
        for statement in _statements(policy)
        for action in _as_list(statement.get("Action"))
        if action.startswith("securityagent:")
    }
    validated = set(profile["code_review"]["verbs"]) | set(profile["pentest"]["verbs"])

    unknown = granted - validated
    assert not unknown, (
        f"policy grants verbs absent from the validated profile: {sorted(unknown)}"
    )


def test_policy_withholds_target_domain_verification(policy):
    """C-6: domain verification is a one-time operator action.

    The nightly must not be able to verify a domain it did not verify itself;
    it may only read verificationStatus and fail closed.
    """
    granted = {
        action
        for statement in _statements(policy)
        for action in _as_list(statement.get("Action"))
    }
    for withheld in (
        "securityagent:CreateTargetDomain",
        "securityagent:VerifyTargetDomain",
    ):
        assert withheld not in granted, (
            f"{withheld} must stay with the operator bootstrap path (C-6)"
        )
    assert "securityagent:BatchGetTargetDomains" in granted, (
        "the nightly must still be able to assert verificationStatus == VERIFIED"
    )


def test_policy_supports_service_created_groups_only_in_its_agent_space(policy):
    """A review creates its own group; an unrelated space must stay denied."""
    from fnmatch import fnmatchcase
    from string import Template

    values = dict(region="us-east-1", account_id="123456789012",
                  staging_bucket="test", staging_prefix="security-agent",
                  log_group_name="/aws/securityagent/adp-dev-nightly",
                  service_log_group_prefix="/aws/securityagent/adp-embark1-secreview")
    rendered = json.loads(Template(json.dumps(policy)).substitute(values))
    group = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/securityagent/adp-embark1-secreview/cr-example"

    def permits(action, resource):
        return any(action in _as_list(statement["Action"]) and
                   any(fnmatchcase(resource, pattern) for pattern in _as_list(statement["Resource"]))
                   for statement in _statements(rendered))

    for action, suffix in (("logs:CreateLogGroup", ""), ("logs:CreateLogGroup", ":*"),
                           ("logs:DescribeLogStreams", ":*"),
                           ("logs:CreateLogStream", ":log-stream:review"),
                           ("logs:PutLogEvents", ":log-stream:review")):
        assert permits(action, group + suffix), (action, suffix)
        assert not permits(action, group.replace("adp-embark1-secreview/", "other-space/") + suffix)
        assert not permits(action, group.replace("adp-embark1-secreview/", "adp-embark1-secreview-other/") + suffix)
        assert not permits(action, group.replace("123456789012", "999999999999") + suffix)
    assert not permits("logs:DeleteLogGroup", group)
    terraform = TERRAFORM_PATH.read_text()
    assert 'jsondecode(file("${path.module}/../../.github/security/security-agent-profile.json"))' in terraform
    assert "local.securityagent_profile.agent_space.existing_name" in terraform


def test_policy_grants_no_iam_or_sts_privilege(policy):
    """A role that can rewrite IAM or assume other roles has no ceiling."""
    for statement in _statements(policy):
        for action in _as_list(statement.get("Action")):
            service = action.split(":", 1)[0]
            assert service not in ("iam", "sts", "organizations", "kms"), (
                f"{action!r} escapes the intended blast radius"
            )


def test_policy_placeholders_are_all_supplied_by_terraform(policy):
    """Every ${...} in the JSON is passed by the templatefile call.

    An unsupplied placeholder is a terraform error; a supplied-but-unused one
    is dead config. Both are caught here rather than at apply time.
    """
    placeholders = set(re.findall(r"\$\{(\w+)\}", json.dumps(policy)))
    terraform = TERRAFORM_PATH.read_text(encoding="utf-8")

    template_block = terraform.split("templatefile(", 1)[1]
    supplied = set(re.findall(r"^\s*(\w+)\s*=", template_block, re.MULTILINE))

    missing = placeholders - supplied
    assert not missing, f"placeholders never supplied by terraform: {sorted(missing)}"


def test_terraform_trusts_only_the_security_agent_service(policy):
    """The trust policy names the service principal and guards against the
    confused-deputy problem."""
    terraform = TERRAFORM_PATH.read_text(encoding="utf-8")
    assert "securityagent.amazonaws.com" in terraform
    assert "aws:SourceAccount" in terraform, (
        "a service-principal trust without a SourceAccount condition lets "
        "another account's resources name this role"
    )


# --------------------------------------------------------------------------
# regression checks
# --------------------------------------------------------------------------


def test_this_suite_is_pinned_into_script_tests():
    """script-tests.yml pins test files explicitly (a glob silently stops
    covering a renamed file). An unpinned suite makes this gate vacuous."""
    text = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_securityagent_preflight.py" in text, (
        "the new suite is not pinned into Script Tests, so it never runs in CI"
    )
    for path in (
        ".github/scripts/securityagent_preflight.py",
        "platform/infra/policies/securityagent-nightly-policy.json",
    ):
        assert path in text, f"{path} is not in the Script Tests paths filter"


def test_pyyaml_is_installed_by_script_tests():
    """This suite parses YAML; the workflow previously installed only pytest.

    Without the dependency every YAML-parsing gate here silently skips
    (importorskip) and the no-schedule assertion never runs in CI.
    """
    text = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
    install = next(line for line in text.splitlines() if "pip install pytest" in line)
    assert "pyyaml" in install.lower(), (
        "Script Tests must install PyYAML or this suite's YAML gates skip"
    )


def _assert_preflight_workflow_scope(changed: set[str]):
    """Protect the scan pipeline; restrict this unit when its artifacts change.

    The scan pipeline is protected by the PROPERTY this unit depends on -- it
    stays dispatch-only -- rather than by forbidding any edit to the file. The
    blanket form over-reached: it also blocked the workflow's chartered owner
    (S19, #5618) from repairing the scanner defects the 2026-09-21 run exposed.
    The dedup suite asserts the same dispatch-only property by content.
    """
    if ".github/workflows/security-scan.yml" in changed:
        scan_path = REPO_ROOT / ".github/workflows/security-scan.yml"
        with scan_path.open(encoding="utf-8") as fh:
            workflow = yaml.safe_load(fh)
        # PyYAML parses the `on:` key as the boolean True.
        triggers = set(workflow[True])
        assert triggers == {"workflow_dispatch"}, (
            "security-scan.yml must stay dispatch-only; found triggers: "
            f"{sorted(triggers)}"
        )
    unit_paths = {str(p.relative_to(REPO_ROOT)) for p in (SCRIPT_PATH, POLICY_PATH, TERRAFORM_PATH)}
    if changed.isdisjoint(unit_paths):
        return
    workflows = {p for p in changed if p.startswith(".github/workflows/")}
    allowed = {
        ".github/workflows/security-agent-nightly.yml",
        ".github/workflows/script-tests.yml",
    }
    assert workflows <= allowed, (
        f"unexpected workflow changes alongside the preflight unit: {sorted(workflows - allowed)}"
    )


def test_existing_security_scan_workflow_is_untouched():
    """Keep security-scan unchanged without blocking unrelated workflow work."""
    completed = subprocess.run(  # nosec B603 - fixed argv, no shell
        ["git", "diff", "--name-only", "origin/main", "--"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip("origin/main not available for comparison")
    _assert_preflight_workflow_scope(set(completed.stdout.splitlines()))


def test_unrelated_workflow_changes_do_not_change_preflight_scope():
    _assert_preflight_workflow_scope({".github/workflows/aidlc-gate-nudge.yml"})


@pytest.mark.parametrize("artifact", [SCRIPT_PATH, POLICY_PATH, TERRAFORM_PATH])
def test_preflight_artifact_changes_still_reject_unrelated_workflows(artifact):
    with pytest.raises(AssertionError, match="unexpected workflow changes"):
        _assert_preflight_workflow_scope({
            str(artifact.relative_to(REPO_ROOT)), ".github/workflows/aidlc-gate-nudge.yml",
        })


def test_preflight_scan_protection_applies_even_without_unit_changes(monkeypatch, tmp_path):
    """Editing the scan workflow is allowed, but reintroducing a non-dispatch
    trigger is not -- the property the guard now enforces.

    The real workflow is dispatch-only, so the guard passes for it. To prove the
    check still bites, point REPO_ROOT at a copy that carries a `pull_request`
    trigger and confirm it is rejected.
    """
    # The live workflow is a legitimate edit: dispatch-only, so no failure.
    _assert_preflight_workflow_scope({".github/workflows/security-scan.yml"})

    bad = tmp_path / ".github/workflows"
    bad.mkdir(parents=True)
    (bad / "security-scan.yml").write_text(
        "name: Security Scan\non:\n  workflow_dispatch:\n  pull_request:\n"
        "    branches: [main]\njobs:\n  noop:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: 'true'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys.modules[__name__], "REPO_ROOT", tmp_path, raising=True
    )
    with pytest.raises(AssertionError, match="dispatch-only"):
        _assert_preflight_workflow_scope({".github/workflows/security-scan.yml"})


def test_preflight_can_update_its_own_workflow_binding():
    _assert_preflight_workflow_scope({
        str(SCRIPT_PATH.relative_to(REPO_ROOT)), ".github/workflows/security-agent-nightly.yml",
    })


def test_no_secret_material_in_the_new_artifacts():
    """The policy and workflow carry references, never credentials."""
    for path in (POLICY_PATH, WORKFLOW_PATH, SCRIPT_PATH, TERRAFORM_PATH):
        blob = path.read_text(encoding="utf-8")
        for marker in ("-----BEGIN", "ghp_", "github_pat_", "AKIA", "ASIA"):
            assert marker not in blob, f"{path.name} appears to contain a secret"
