"""Gate for the nightly whole-repo code-review driver (U5, issue #4445).

Every assertion here exists because a specific failure has a named blast radius
in the unit's impact analysis:

  - a reachable ``validationMode``   -> the review starts exercising live
                                        endpoints, giving this job the blast
                                        radius of the pentest half without any
                                        visible change to the workflow
  - a reachable remediation strategy -> the service opens its own fix PRs,
                                        racing the fix pipeline this EPIC
                                        builds: two competing fixes per finding
  - a selectable profile SOURCE      -> the same two settings, reached by a
                                        longer route: whoever names the profile
                                        file names the values it records, with
                                        no schema check at load time and no
                                        diff to the driver (#4524)
  - an illegal title                 -> the create call is rejected and the
                                        night produces nothing, with an error
                                        that reads like a service outage
  - a non-zip / fat archive          -> the service rejects the asset, or review
                                        time and cost balloon on vendored code
  - create before registration       -> "Service role ... not found in agent
                                        instance IAM roles", which reads like
                                        broken IAM and is not
  - a timeout that merely returns    -> a metered job keeps running with nothing
                                        holding its id: unabortable in practice
  - a bound below measured reality   -> every night bills an hour of review,
                                        aborts a healthy job ~2.5h early and
                                        retains zero findings, with an error
                                        that reads like a service outage
  - a bound above the runner's       -> the runner kills the step before the
                                        abort fires, leaving the job running
                                        and unabortable: the exact failure the
                                        abort exists to prevent
  - findings outside the IAM prefix  -> PutObject AccessDenied after the review
                                        has been polled to completion and
                                        billed: full cost, zero findings

Why unreachability is asserted with ``ast`` and not with ``grep``
----------------------------------------------------------------
The obvious test is "the permissive literals do not appear in the source". That
test is wrong, and it fails on a correct driver: ``SIMULATED`` and ``AUTOMATIC``
appear in ``code_review_request.py``'s own comments, which explain why they are
dangerous. The tempting way to make a textual test pass is deleting those
comments, which makes the code strictly worse.

So unreachability is asserted structurally: parse the module and check that no
function takes either setting as a parameter and no CLI flag sets one. That is
the actual safety property -- a keyword argument defaulting to the safe value is
still reachable by a later caller, whereas a constant with no parameter cannot be
reached without editing the file, which is a reviewable diff.

Why the profile SOURCE is a forbidden lever too
-----------------------------------------------
The structural check above models only *direct* levers, and that made it weaker
than it read (#4524). The driver used to read both values out of the profile
while ``--profile`` chose which file the profile was; the file is parsed with a
bare ``json.loads`` and validated against no schema at load time. So neither
setting appeared as a parameter -- the gate passed -- yet both were reachable
from a workflow edit, which is exactly the class of change this unit exists to
prevent. A lever that selects the *source* of a pinned value is a lever on the
value, so this file treats it as forbidden alongside the setting names
themselves. The values now live in the driver as constants and the profile is
asserted against them, so there is no file to point anywhere.
"""

import ast
import re
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from code_review_request import (  # noqa: E402
    ARCHIVE_SUFFIX,
    DEFAULT_POLL_TIMEOUT_SECONDS,
    EXCLUDED_DIRS,
    EXCLUDED_FILE_PATTERNS,
    PINNED_MODES,
    TERMINAL_JOB_STATUSES,
    CodeReviewError,
    assert_title_is_legal,
    build_parser,
    build_source_archive,
    collect_findings,
    load_profile,
    nightly_title,
    pinned_modes,
    poll_until_terminal,
    run_code_review,
    staging_object_key,
    write_findings,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "code_review_request.py"
NIGHTLY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security-agent-nightly.yml"
PUBLISH_ACTION = (
    REPO_ROOT / ".github" / "actions" / "publish-findings-s3" / "action.yml"
)
SECURITYAGENT_IAM_TF = (
    REPO_ROOT / "platform" / "infra" / "securityagent-nightly-iam.tf"
)

# The two settings whose permissive members change what the service DOES.
# Spelled in both the API's camelCase and the snake_case a Python parameter
# would plausibly use, because either spelling would be a reachable path.
FORBIDDEN_MODE_NAMES = {
    "validation_mode",
    "validationMode",
    "remediation_strategy",
    "remediationStrategy",
    "code_remediation_strategy",
    "codeRemediationStrategy",
}

# Levers that select which file the profile is read FROM. Forbidden for the same
# reason as the names above and with the same blast radius: the profile records
# the pinned modes, is parsed with a bare `json.loads`, and is validated by no
# schema at load time -- so choosing the file chooses the values (#4524).
#
# Note the asymmetry between the two gates that consume this set, which is not an
# oversight:
#
#   * As a CLI FLAG, bare `--profile` is forbidden. A flag can only ever carry a
#     path, so the name is unambiguous -- and `--profile` is the exact flag this
#     defect was about.
#   * As a FUNCTION PARAMETER, bare `profile` is legitimate and pervasive: it is
#     the already-loaded dict that `pinned_modes(profile)` and friends take, and
#     that dict is checked against the driver's constants rather than trusted.
#     Only the path-shaped spellings are forbidden there, via
#     FORBIDDEN_PROFILE_PATH_PARAMS below.
#
# Neither list can enumerate every synonym, so they are a ratchet against
# reintroducing the known shape, not a proof. The general case is closed by
# `test_load_profile_takes_no_path_argument`: the one function that reads the
# file accepts no path at all, so there is nothing for a flag to feed.
FORBIDDEN_PROFILE_SOURCE_FLAGS = {
    "profile",
    "profile_path",
    "profile_file",
    "profile_json",
    "profile_source",
}

# The path-shaped spellings, forbidden as parameters. Bare `profile` is
# deliberately absent -- see the comment above.
FORBIDDEN_PROFILE_PATH_PARAMS = {
    "profile_path",
    "profilePath",
    "profile_file",
    "profileFile",
    "profile_json",
    "profile_source",
}

# What the parameter-level ast gate forbids.
FORBIDDEN_PARAM_NAMES = FORBIDDEN_MODE_NAMES | FORBIDDEN_PROFILE_PATH_PARAMS

# What the CLI-level ast gate forbids.
FORBIDDEN_FLAG_NAMES = FORBIDDEN_MODE_NAMES | FORBIDDEN_PROFILE_SOURCE_FLAGS

# The prefix the nightly role's inline policy confines s3:PutObject to.
# Read from Terraform rather than retyped -- see the test that asserts this.
REQUIRED_FINDINGS_PREFIX = "security-agent"


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def profile():
    return load_profile()


@pytest.fixture(scope="module")
def module_ast():
    """The driver, parsed. Parsed rather than grepped: a substring search
    cannot tell a parameter from a word in a comment, which is the entire
    difficulty this file's central assertion has to navigate."""
    assert SCRIPT_PATH.is_file(), f"driver missing: {SCRIPT_PATH}"
    return ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))


class _RecordingClient:
    """Records call order and returns canned responses.

    Order matters more than payloads here: UpdateAgentSpace must precede
    CreateCodeReview, and that is an ordering the API enforces with an error
    message that points at IAM instead.
    """

    def __init__(self, statuses=None, findings_pages=None, findings=None):
        self.calls: list[str] = []
        self.kwargs: dict[str, dict] = {}
        self._statuses = list(statuses or ["COMPLETED"])
        self._findings_pages = list(findings_pages or [])
        self._findings = list(findings or [])

    def _record(self, call_name, /, **kwargs):
        # Positional-only: UpdateAgentSpace legitimately passes `name=`, which
        # would otherwise collide with this method's own parameter.
        self.calls.append(call_name)
        self.kwargs[call_name] = kwargs

    def update_agent_space(self, **kwargs):
        self._record("update_agent_space", **kwargs)
        return {}

    def create_code_review(self, **kwargs):
        self._record("create_code_review", **kwargs)
        return {"codeReviewId": "cr-test"}

    def start_code_review_job(self, **kwargs):
        self._record("start_code_review_job", **kwargs)
        return {"codeReviewJobId": "job-test"}

    def batch_get_code_review_jobs(self, **kwargs):
        self._record("batch_get_code_review_jobs", **kwargs)
        status = self._statuses.pop(0) if self._statuses else "IN_PROGRESS"
        return {"codeReviewJobs": [{"status": status}]}

    def stop_code_review_job(self, **kwargs):
        self._record("stop_code_review_job", **kwargs)
        return {}

    def list_findings(self, **kwargs):
        self._record("list_findings", **kwargs)
        return self._findings_pages.pop(0) if self._findings_pages else {}

    def batch_get_findings(self, **kwargs):
        self._record("batch_get_findings", **kwargs)
        return {"findings": self._findings}


class _RecordingS3:
    def __init__(self):
        self.uploads: list[tuple[str, str, str]] = []

    def upload_file(self, filename, bucket, key):
        self.uploads.append((str(filename), bucket, key))


# --------------------------------------------------------------------------
# 1 + 2. the two pinned settings are UNREACHABLE
# --------------------------------------------------------------------------


def test_no_function_accepts_either_pinned_setting_as_a_parameter(module_ast):
    """The core safety property, asserted structurally.

    Covers positional, keyword-only, *args/**kwargs-adjacent and defaulted
    parameters: a keyword argument defaulting to the safe value is STILL
    reachable, because a later caller can pass the other value and nothing in
    the driver would notice.

    Also covers path-shaped ``profile_path``-style parameters, which reach the
    same two settings indirectly (#4524). Bare ``profile`` is excluded and must
    stay excluded: that is the already-loaded dict, which the driver checks
    against its own constants rather than trusting.
    """
    offenders = []
    for node in ast.walk(module_ast):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        spec = node.args
        every_arg = [
            *spec.posonlyargs,
            *spec.args,
            *spec.kwonlyargs,
            *([spec.vararg] if spec.vararg else []),
            *([spec.kwarg] if spec.kwarg else []),
        ]
        for arg in every_arg:
            if arg.arg in FORBIDDEN_PARAM_NAMES:
                offenders.append(f"{node.name}(... {arg.arg} ...) at line {node.lineno}")

    assert not offenders, (
        "no function may accept the live-validation mode, the remediation "
        "strategy, or a path to the profile that records them as a parameter -- a "
        "parameter is reachable from a caller, and the permissive member of "
        "either setting turns this code review into something with the pentest's "
        f"blast radius. Offenders: {offenders}"
    )


def test_no_cli_flag_can_set_either_pinned_setting(module_ast):
    """No argparse flag reaches either setting, directly or via the profile file.

    A flag is the cheapest possible path to a permissive value: it needs only a
    workflow edit, not a diff to this file. Inspects every
    ``parser.add_argument`` call's option strings and its ``dest``.

    ``--profile`` counts (#4524). It never named a mode, so this gate used to
    pass with it present -- but it named the file the modes were read out of,
    which is the same reachability with an extra hop.
    """
    offenders = []
    for node in ast.walk(module_ast):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue

        for arg in node.args:
            if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                continue
            # --validation-mode -> validation_mode
            normalised = arg.value.lstrip("-").replace("-", "_")
            if normalised in FORBIDDEN_FLAG_NAMES or normalised.replace(
                "_", ""
            ).lower() in {n.replace("_", "").lower() for n in FORBIDDEN_FLAG_NAMES}:
                offenders.append(f"{arg.value} at line {node.lineno}")

        for kw in node.keywords:
            if (
                kw.arg == "dest"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value in FORBIDDEN_FLAG_NAMES
            ):
                offenders.append(f"dest={kw.value.value!r} at line {node.lineno}")

    assert not offenders, (
        "no CLI flag may set the live-validation mode or the remediation "
        "strategy, or select the profile file that records them; a flag makes the "
        "permissive value reachable from a workflow edit rather than a reviewable "
        f"diff to the driver. Offenders: {offenders}"
    )


def test_the_parser_really_is_the_one_under_test():
    """Guards the two ast tests above against becoming vacuous.

    If the driver were refactored so its flags were no longer registered via
    ``add_argument`` literals, the ast tests would pass by finding nothing. This
    builds the real parser and asserts the forbidden options are rejected while
    a known-good one is accepted.
    """
    parser = build_parser()
    known = {action.dest for action in parser._actions}

    assert not known & FORBIDDEN_FLAG_NAMES, (
        f"parser exposes a forbidden destination: {known & FORBIDDEN_FLAG_NAMES}"
    )
    # Sanity: the parser is real and does carry the flags the workflow passes.
    assert {"run_date", "output"} <= known, (
        f"parser does not look like the driver's; found {sorted(known)}"
    )

    for flag in ("--validation-mode", "--remediation-strategy"):
        with pytest.raises(SystemExit):
            parser.parse_args([flag, "SIMULATED"])


def test_no_flag_can_select_an_alternate_profile(capsys):
    """The indirection this defect was about, closed at the parser (#4524).

    ``--profile`` carried no mode name, so the ast gates passed while the pinned
    values were still selectable: point the driver at another JSON file and the
    "pinned" value is whatever that file says. The parser must now reject it.

    Asserted on the built parser rather than only on the ast, because this is the
    behaviour that matters -- an argparse prefix match or a re-added alias would
    make the source-level check pass while the flag still parsed.
    """
    parser = build_parser()

    for flag in ("--profile", "--profile-path", "--profile-file"):
        with pytest.raises(SystemExit):
            parser.parse_args([flag, "/tmp/attacker-profile.json"])  # nosec B108
        capsys.readouterr()  # argparse writes usage to stderr on reject

    # And the driver's real invocation still parses: the nightly passes exactly
    # these two flags, so closing the indirection must not break the caller.
    args = parser.parse_args(["--run-date", "2026-08-30", "--output", "out.json"])
    assert args.run_date == "2026-08-30"
    assert args.output == "out.json"


def test_load_profile_takes_no_path_argument():
    """The general case behind the flag gate: nothing to feed a path to.

    The named-flag lists above are a ratchet against the known shape and cannot
    enumerate every synonym. This closes the class instead: the single function
    that reads the profile off disk accepts no path at all, so re-adding a flag
    would not be enough to redirect it -- ``load_profile`` itself would have to
    change, in the same reviewable diff.
    """
    import inspect  # noqa: PLC0415

    parameters = inspect.signature(load_profile).parameters
    assert not parameters, (
        "load_profile must take no arguments: a path parameter is a lever on "
        f"every value the profile supplies, including the two pinned modes. "
        f"Found {sorted(parameters)}"
    )


def test_pinned_modes_returns_the_disabled_members_for_the_shipped_profile(profile):
    """``pinned_modes`` has no lever, and the shipped profile agrees with it."""
    modes = pinned_modes(profile)
    assert modes == {
        "validationMode": "DISABLED",
        "codeRemediationStrategy": "DISABLED",
    }


def test_the_pinned_constants_are_the_disabled_members():
    """The values are constants in the driver now, not profile data (#4524).

    Asserted directly so the pin is checked even if `pinned_modes` were later
    rewritten: these two literals ARE the safety property, and the module they
    live in is the only place a diff can change them.
    """
    assert PINNED_MODES == {
        "validationMode": "DISABLED",
        "codeRemediationStrategy": "DISABLED",
    }


def test_pinned_modes_refuses_to_omit_either_setting():
    """A create call omitting either field gets the SERVICE-side default, and
    the profile records that default as the permissive member. Silently sending
    an incomplete call is therefore worse than failing."""
    with pytest.raises(CodeReviewError) as excinfo:
        pinned_modes({"code_review": {"pinned_modes": {"validationMode": "DISABLED"}}})
    assert "codeRemediationStrategy" in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "permissive"),
    [
        ("validationMode", "SIMULATED"),
        ("codeRemediationStrategy", "AUTOMATIC"),
    ],
)
def test_a_profile_disagreeing_with_the_constants_fails_closed(field, permissive):
    """The fail-closed half of the fix (#4524).

    The driver's constants are authority and the profile is record. If a profile
    records the permissive member, the run must FAIL rather than quietly sending
    the safe constant: a profile that disagrees means the artifact has stopped
    describing what the service receives, and which of the two is wrong is not
    something this driver can decide.
    """
    modes = dict(PINNED_MODES)
    modes[field] = permissive

    with pytest.raises(CodeReviewError) as excinfo:
        pinned_modes({"code_review": {"pinned_modes": modes}})

    message = str(excinfo.value)
    assert field in message
    assert permissive in message, (
        "the error must name the value it refused, or the operator cannot tell "
        "which field drifted"
    )


def test_the_sent_values_come_from_the_constants_not_the_profile():
    """A profile cannot influence the returned values, only whether it raises.

    This is what makes the removed ``--profile`` flag harmless even if some other
    path ever loads a different file: the values are not taken from it.
    """
    agreeing = {"code_review": {"pinned_modes": dict(PINNED_MODES)}}
    returned = pinned_modes(agreeing)

    assert returned == PINNED_MODES
    # Returned dict must be a copy: a caller mutating it must not edit the pin.
    returned["validationMode"] = "MUTATED"
    assert PINNED_MODES["validationMode"] == "DISABLED"


def test_create_review_sends_both_settings_disabled(profile):
    """End-to-end on the actual create call: both settings leave the process
    pinned to DISABLED."""
    client = _RecordingClient()
    client.update_agent_space(agentSpaceId="as-x", name="n")
    from code_review_request import create_review  # noqa: PLC0415

    create_review(client, profile, "as-x", "adp-dev-nightly-codereview-20260830", "s3://b/k.zip")

    sent = client.kwargs["create_code_review"]
    assert sent["validationMode"] == "DISABLED"
    assert sent["codeRemediationStrategy"] == "DISABLED"


# --------------------------------------------------------------------------
# 3. title
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_date",
    ["2026-01-01", "2026-08-30", "2026-12-31", "2027-02-28", "2026-06-15"],
)
def test_title_satisfies_the_recorded_charset(run_date, profile):
    """Property test over a range of dates, against the charset READ from the
    profile. The API rejects spaces and colons, so a title built from an issue
    title or an ISO timestamp always fails -- hence generated, not passed in."""
    title = nightly_title(run_date)
    pattern = profile["code_review"]["title_charset"]

    assert re.fullmatch(pattern, title), f"{title!r} violates {pattern}"
    assert len(title) <= 100
    assert " " not in title and ":" not in title
    assert title == f"adp-dev-nightly-codereview-{run_date.replace('-', '')}"


def test_title_charset_is_read_from_the_profile_not_retyped():
    """The charset literal must not appear in the driver: two copies would let
    the profile change while the generator kept validating against the old one."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "[A-Za-z0-9_-]{1,100}" not in source, (
        "the title charset is retyped in the driver; it must be read from the "
        "profile so a change to the recorded charset cannot drift"
    )


def test_an_illegal_title_fails_locally_rather_than_at_the_service(profile):
    """A malformed title must fail before the request leaves the process, so the
    cause is legible instead of looking like a service outage."""
    with pytest.raises(CodeReviewError):
        assert_title_is_legal("nightly review: 2026-08-30", profile)


def test_a_non_digit_date_is_rejected():
    with pytest.raises(CodeReviewError):
        nightly_title("not-a-date")


# --------------------------------------------------------------------------
# 4. archive is a zip, and excludes deps / build output
# --------------------------------------------------------------------------


def _seed_repo(root: Path):
    """A tree carrying one real source file and one file in each exclusion
    class named by the unit's impact analysis."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("print('real source')\n", encoding="utf-8")

    for directory in ("node_modules", ".venv", "dist", "__pycache__", ".terraform"):
        (root / directory).mkdir()
        (root / directory / "junk.py").write_text("junk\n", encoding="utf-8")

    (root / "terraform.tfstate").write_text("{}\n", encoding="utf-8")
    (root / "src" / "b.pyc").write_bytes(b"\x00")


def test_archive_is_a_zip_and_excludes_every_exclusion_class(tmp_path):
    """Zip specifically: the generic ``s3Location`` field name suggests any
    archive works and the service rejects everything else. Exclusions because
    packaging dependencies balloons review time and cost on vendored code and
    surfaces findings in files nobody in this repo owns.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    archive = tmp_path / "src.zip"

    build_source_archive(repo, archive)

    assert archive.suffix == ARCHIVE_SUFFIX
    assert zipfile.is_zipfile(archive), "the service rejects non-zip source assets"

    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()

    assert names == ["src/a.py"], f"archive should hold only real source; got {names}"
    for excluded in ("node_modules", ".venv", "dist", "__pycache__", ".terraform"):
        assert not any(excluded in n for n in names), f"{excluded} leaked into the archive"
    assert not any(n.endswith(".pyc") for n in names)
    assert not any(".tfstate" in n for n in names)


def test_a_non_zip_destination_is_rejected(tmp_path):
    """The failure must happen at packaging time, not when the service rejects
    the asset after the upload has already been paid for."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)

    with pytest.raises(CodeReviewError) as excinfo:
        build_source_archive(repo, tmp_path / "src.tar.gz")
    assert ".zip" in str(excinfo.value)


def test_an_empty_package_is_refused(tmp_path):
    """Zero files would submit a review that reports zero findings and looks
    exactly like a clean repo."""
    repo = tmp_path / "empty"
    repo.mkdir()
    with pytest.raises(CodeReviewError):
        build_source_archive(repo, tmp_path / "src.zip")


def test_exclusions_have_a_single_source_of_truth():
    """The packer and this gate must read the same tuples. Two lists would let
    the gate pass while the packer shipped node_modules."""
    for required in ("node_modules", ".venv", "dist", "__pycache__", ".terraform"):
        assert required in EXCLUDED_DIRS
    assert any("tfstate" in p for p in EXCLUDED_FILE_PATTERNS)


# --------------------------------------------------------------------------
# 5. registration strictly precedes creation
# --------------------------------------------------------------------------


def test_registration_precedes_creation(tmp_path, profile):
    """UpdateAgentSpace must land BEFORE CreateCodeReview.

    Skipping or reordering the registration produces "Service role ... not found
    in agent instance IAM roles" at create time, which reads like a broken IAM
    policy and is not one.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_repo(repo)
    client = _RecordingClient(statuses=["COMPLETED"])

    run_code_review(
        client,
        _RecordingS3(),
        profile,
        repo_root=repo,
        archive_path=tmp_path / "src.zip",
        output_path=tmp_path / "out.json",
        run_date="2026-08-30",
        clock=lambda: 0.0,
        sleeper=lambda _s: None,
    )

    assert "update_agent_space" in client.calls
    assert "create_code_review" in client.calls
    assert client.calls.index("update_agent_space") < client.calls.index(
        "create_code_review"
    ), f"registration must precede creation; call order was {client.calls}"
    # And the job is started only after it has been created.
    assert client.calls.index("create_code_review") < client.calls.index(
        "start_code_review_job"
    )


def test_findings_are_scoped_to_this_job(profile):
    """ListFindings unscoped returns the agent space's whole history rather than
    this night's findings, which would silently mix old findings into the
    handoff the dedup unit consumes."""
    client = _RecordingClient(
        findings_pages=[{"findingsSummaries": [{"findingId": "f-1"}]}],
        findings=[{"findingId": "f-1"}],
    )
    collect_findings(client, profile, "as-x", "job-test")

    assert client.kwargs["list_findings"]["codeReviewJobId"] == "job-test"
    assert client.kwargs["list_findings"]["agentSpaceId"] == "as-x"
    # BatchGetCodeReviewJobs/BatchGetFindings need agentSpaceId too; the
    # runbook's example omits it and the call fails.
    assert client.kwargs["batch_get_findings"]["agentSpaceId"] == "as-x"


# --------------------------------------------------------------------------
# 6. the poll timeout STOPS the job
# --------------------------------------------------------------------------


def test_poll_timeout_stops_the_job_and_raises():
    """The abort is the point of the bound, not a tidy-up.

    Merely returning would leave the workflow to hang to its runner timeout
    while a metered job kept running with nothing left holding its id --
    unabortable in practice.
    """
    client = _RecordingClient(statuses=["IN_PROGRESS", "IN_PROGRESS", "IN_PROGRESS"])
    ticks = iter([0.0, 10.0, 999.0, 999.0])

    with pytest.raises(CodeReviewError) as excinfo:
        poll_until_terminal(
            client,
            "as-x",
            "job-test",
            timeout_seconds=60,
            interval_seconds=1,
            clock=lambda: next(ticks),
            sleeper=lambda _s: None,
        )

    assert "stop_code_review_job" in client.calls, (
        "a timeout that does not call StopCodeReviewJob abandons a metered job"
    )
    assert client.kwargs["stop_code_review_job"] == {
        "agentSpaceId": "as-x",
        "codeReviewJobId": "job-test",
    }
    assert "did not reach a terminal state" in str(excinfo.value)


def _review_step_timeout_minutes():
    """The `timeout-minutes` on the nightly step that runs the driver.

    Located by finding the step whose `run` invokes the driver rather than by
    index or name: a step reordered or renamed would otherwise silently move
    these gates onto the wrong timeout, which is exactly the drift they exist to
    catch.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML required to parse the workflow")
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))

    steps = [
        step
        for step in workflow["jobs"]["code-review"]["steps"]
        if "code_review_request.py" in (step.get("run") or "")
    ]
    assert len(steps) == 1, (
        "expected exactly one step invoking the driver; found "
        f"{len(steps)}, so the timeout these gates check is ambiguous"
    )

    timeout = steps[0].get("timeout-minutes")
    assert timeout is not None, (
        "the review step has no timeout-minutes at all. Without it the job's "
        "implicit 360m ceiling becomes the killer, and a runner kill leaves the "
        "metered job running with nothing holding its id"
    )
    return int(timeout)


def test_the_poll_bound_fires_before_the_runner_kills_the_step():
    """The ordering invariant, parsed from both files rather than asserted as
    literals -- so it fails if either value is later changed alone.

    The script's bound MUST expire first. If the runner kills the step first the
    job is abandoned rather than stopped: nothing is left holding its id, so it
    is unabortable in practice and keeps metering. That is the failure
    ``test_poll_timeout_stops_the_job_and_raises`` proves the code prevents, and
    this gate proves the configuration does not undo it.
    """
    runner_bound_seconds = _review_step_timeout_minutes() * 60

    assert DEFAULT_POLL_TIMEOUT_SECONDS < runner_bound_seconds, (
        f"the driver's poll bound ({DEFAULT_POLL_TIMEOUT_SECONDS}s) must be "
        f"STRICTLY less than the review step's timeout-minutes "
        f"({runner_bound_seconds}s) so StopCodeReviewJob is reached before the "
        "runner kills the step. Raising one without the other abandons a "
        "metered, unabortable job"
    )


def test_the_poll_bound_exceeds_every_measured_run(profile):
    """A bound below observed reality aborts healthy jobs and retains nothing.

    This is defect #4526 itself: the original 3600s bound was set before any
    whole-repo review had been timed, so every night paid for an hour of metered
    review, called StopCodeReviewJob about 2.5h early, and published an empty
    findings document. Reading the durations from the profile means a future
    reduction below what has actually been measured fails here.
    """
    measured = profile["code_review"]["observed_job_durations"]
    assert measured, (
        "no measured durations recorded, so this gate would pass vacuously and "
        "the bound would be an invented number again"
    )

    longest = max(measured, key=lambda run: run["duration_seconds"])

    assert DEFAULT_POLL_TIMEOUT_SECONDS > longest["duration_seconds"], (
        f"the poll bound ({DEFAULT_POLL_TIMEOUT_SECONDS}s) does not exceed the "
        f"longest measured run ({longest['duration_seconds']}s, "
        f"{longest['job_title']}). A bound below observed reality aborts a "
        "healthy job after paying for it and retains zero findings (#4526)"
    )


def test_the_measured_durations_are_derived_from_their_own_timestamps(profile):
    """Guards the gate above from a mistyped duration.

    ``duration_seconds`` is what the bound is checked against, but the
    timestamps are the evidence. If they disagree, the recorded number is not a
    measurement of anything and the bound rests on a typo.
    """
    from datetime import datetime  # noqa: PLC0415

    for run in profile["code_review"]["observed_job_durations"]:
        created = run.get("created_at")
        terminal = run.get("terminal_at")
        if not (created and terminal):
            continue
        span = (
            datetime.fromisoformat(terminal.replace("Z", "+00:00"))
            - datetime.fromisoformat(created.replace("Z", "+00:00"))
        ).total_seconds()
        assert span == run["duration_seconds"], (
            f"{run['job_title']}: recorded duration {run['duration_seconds']}s "
            f"does not match its own timestamps ({span:.0f}s)"
        )


@pytest.mark.parametrize("terminal", ["COMPLETED", "FAILED", "STOPPED"])
def test_a_terminal_status_returns_without_stopping(terminal):
    client = _RecordingClient(statuses=[terminal])
    assert (
        poll_until_terminal(
            client, "as-x", "job-test", clock=lambda: 0.0, sleeper=lambda _s: None
        )
        == terminal
    )
    assert "stop_code_review_job" not in client.calls


def test_terminal_statuses_are_a_subset_of_the_recorded_enum(profile):
    """A service that grows a new terminal state must fail this gate rather
    than be polled forever."""
    recorded = set(profile["code_review"]["enums"]["job_status"])
    assert set(TERMINAL_JOB_STATUSES) <= recorded


# --------------------------------------------------------------------------
# the findings object must land inside the prefix the nightly MAY write
# --------------------------------------------------------------------------


def test_the_nightly_publishes_inside_the_iam_permitted_prefix():
    """Defect 3 of #4517, and the one with real cost attached.

    The nightly role's inline policy grants s3:PutObject only under
    ``security-agent/*``. The publish action's DEFAULT layout is
    ``findings/<run_id>/<tool>/``, which is outside it -- so without an explicit
    dest-prefix the publish fails AccessDenied after the review has been polled
    to completion and billed. Full cost, zero retained findings.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML required to parse the workflow")
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))

    publish = [
        step
        for step in workflow["jobs"]["code-review"]["steps"]
        if "publish-findings-s3" in (step.get("uses") or "")
    ]
    assert len(publish) == 1, "the code-review job must publish exactly once"

    dest_prefix = publish[0]["with"]["dest-prefix"]
    assert dest_prefix.startswith(f"{REQUIRED_FINDINGS_PREFIX}/"), (
        f"findings destination {dest_prefix!r} is outside the only prefix the "
        f"nightly role may write to ({REQUIRED_FINDINGS_PREFIX}/*)"
    )
    # #4445's Validation and the wave-3 eval both expect the dated runs layout.
    assert dest_prefix.startswith(f"{REQUIRED_FINDINGS_PREFIX}/runs/")
    assert "run-date" in dest_prefix.lower() or "RUN_DATE" in dest_prefix, (
        f"the findings prefix must be dated; got {dest_prefix!r}"
    )
    assert dest_prefix.endswith("/"), (
        "a destination without a trailing slash is treated by `aws s3 cp` as an "
        "object key, writing the findings TO the prefix rather than into it"
    )


def test_the_permitted_prefix_is_read_from_terraform_not_assumed():
    """Guards the test above from asserting against a prefix this file invented.
    If Terraform's prefix changes, this fails rather than letting the gate keep
    checking a stale value."""
    tf = SECURITYAGENT_IAM_TF.read_text(encoding="utf-8")
    assert f'securityagent_staging_prefix = "{REQUIRED_FINDINGS_PREFIX}"' in tf, (
        f"Terraform no longer sets the staging prefix to "
        f"{REQUIRED_FINDINGS_PREFIX!r}; this gate is now checking a stale value"
    )


def test_the_drivers_own_upload_also_stays_inside_the_prefix():
    """The upload path already got this right; assert it so it stays right."""
    key = staging_object_key("adp-dev-nightly-codereview-20260830")
    assert key.startswith(f"{REQUIRED_FINDINGS_PREFIX}/")
    assert key.endswith(ARCHIVE_SUFFIX)


def test_publish_action_default_preserves_the_legacy_destination():
    """The 8 existing security-scan.yml callers must not change behaviour.

    ``dest-prefix`` is optional and defaults to empty, and the action falls back
    to the legacy ``findings/<run_id>/<tool-name>/`` layout when it is empty.
    Those callers are byte-identical to main (an asserted #4445 regression
    check), so a required input or a changed default would break all 8.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML required to parse the action")
    action = yaml.safe_load(PUBLISH_ACTION.read_text(encoding="utf-8"))

    spec = action["inputs"]["dest-prefix"]
    assert spec.get("required") is False, "dest-prefix must stay optional"
    assert spec.get("default") == "", "an empty default is what selects the legacy path"

    body = action["runs"]["steps"][0]["run"]
    assert 'findings/${{ github.run_id }}/${{ inputs.tool-name }}/' in body, (
        "the legacy destination must remain the fallback for callers that pass "
        "no dest-prefix"
    )


def test_the_new_suite_is_pinned_in_the_script_tests_gate():
    """A test file that CI never runs makes the cited gate vacuous.

    script-tests.yml pins its suite list explicitly rather than globbing (a
    glob silently stops covering a renamed file), so a new suite must be added
    there by hand or it never executes.
    """
    workflow = (
        REPO_ROOT / ".github" / "workflows" / "script-tests.yml"
    ).read_text(encoding="utf-8")
    assert "tests/test_code_review_request.py" in workflow, (
        "this suite is not pinned in script-tests.yml, so CI does not run it"
    )


def test_the_nightly_still_has_no_schedule_key():
    """U5 must not arm the schedule: that needs a measured run duration and a
    window that does not collide with three existing nightly suites."""
    yaml = pytest.importorskip("yaml", reason="PyYAML required to parse the workflow")
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML follows YAML 1.1, where the bare key `on` is boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert triggers is not None
    assert "schedule" not in triggers, "no schedule key: see the header of the workflow"


# --------------------------------------------------------------------------
# the handoff document
# --------------------------------------------------------------------------


def test_findings_are_written_verbatim(tmp_path):
    """No normalisation, dedup or severity remapping here: that is the consuming
    unit's job, and doing any of it would mean dedup could never see what the
    service actually said."""
    import json  # noqa: PLC0415

    findings = [{"findingId": "f-1", "riskType": "SQL_INJECTION", "name": "n"}]
    out = tmp_path / "findings.json"

    write_findings(
        out,
        findings=findings,
        run_date="2026-08-30",
        title="adp-dev-nightly-codereview-20260830",
        agent_space_id="as-x",
        code_review_id="cr-1",
        job_id="job-1",
        status="COMPLETED",
    )

    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["findings"] == findings
    assert document["findingCount"] == 1
    assert document["runDate"] == "2026-08-30"
    assert document["jobStatus"] == "COMPLETED"
