"""Gate for the findings-bucket privacy assertion (U5 #4445, defect #4533).

The check this replaces (``aws s3api get-bucket-policy-status``) failed 100% of
runs on the deployed target with ``NoSuchBucketPolicy``, because the findings
bucket deliberately has no bucket policy at all. So the pass condition of the
intent's "findings are never public" criterion was unreachable, and the
criterion went unverified.

Every assertion below maps to a row of the defect's blast-radius table:

  - fixed by removing the check        -> privacy unverified; a future
                                          bucket-policy change that opens the
                                          bucket ships unnoticed
  - swallows all errors (``|| true``)  -> green forever, including on a
                                          genuinely public bucket. This is the
                                          vacuous-gate failure #4517 already
                                          hit on this EPIC, so the ability to
                                          go RED is asserted directly
  - asserts only the bucket policy     -> a bucket opened by a removed
                                          public-access-block reads as private
  - hard-fails on NoSuchBucketPolicy   -> healthy nightly runs halted by a
                                          tooling artifact (today's behaviour)

No test here touches AWS: the S3 client is injected, and the error paths raise
real ``botocore.exceptions.ClientError`` objects so the production code is
exercised against the exception type it will actually see.
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from assert_findings_bucket_private import (  # noqa: E402
    REQUIRED_PUBLIC_ACCESS_BLOCK_FLAGS,
    BucketPrivacyError,
    assert_bucket_is_private,
    assert_bucket_policy_not_public,
    assert_public_access_block,
    build_parser,
    main,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = (
    REPO_ROOT / ".github" / "scripts" / "assert_findings_bucket_private.py"
)
NIGHTLY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security-agent-nightly.yml"
SCRIPT_TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "script-tests.yml"
SCAN_IAM_TF = REPO_ROOT / "platform" / "automation-infra" / "scan-dispatch.tf"

BUCKET = "adp-dev-security-scans-000000000000"

# Spelled out here rather than imported, deliberately. The parametrised
# per-flag test below iterates the production constant, so narrowing that
# constant would delete test cases rather than fail them; this pins the set
# independently so a dropped flag is a failure and not a silent loss of
# coverage.
EXPECTED_FLAGS = frozenset(
    {
        "BlockPublicAcls",
        "IgnorePublicAcls",
        "BlockPublicPolicy",
        "RestrictPublicBuckets",
    }
)

# The two read-only calls the assertion makes. The nightly step runs as the ARC
# runner identity (the code-review job has no configure-aws-credentials step),
# so these are the grants that must exist on the runner role.
REQUIRED_RUNNER_ACTIONS = (
    "s3:GetBucketPublicAccessBlock",
    "s3:GetBucketPolicyStatus",
)


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------


def client_error(code, operation):
    """A real botocore ClientError, so the code under test is exercised
    against the exception type and shape AWS actually raises rather than a
    hand-rolled stand-in whose ``response`` dict might not match."""
    botocore = pytest.importorskip(
        "botocore.exceptions", reason="botocore required to build a ClientError"
    )
    return botocore.ClientError(
        {"Error": {"Code": code, "Message": f"stubbed {code}"}}, operation
    )


def all_flags(value=True):
    """A public-access-block response body. Built from EXPECTED_FLAGS so the
    stub keeps modelling the real bucket even if the production constant is
    narrowed."""
    return {flag: value for flag in sorted(EXPECTED_FLAGS)}


class StubS3:
    """Returns canned responses, or raises, per call.

    Also records which calls were made, because "the policy leg never ran" is a
    way for this assertion to pass without having checked what it claims.
    """

    def __init__(self, public_access_block=None, policy_status=None):
        self._pab = public_access_block
        self._policy = policy_status
        self.calls = []

    def get_public_access_block(self, Bucket):  # noqa: N803 - boto3 kwarg casing
        self.calls.append(("get_public_access_block", Bucket))
        if isinstance(self._pab, Exception):
            raise self._pab
        return {"PublicAccessBlockConfiguration": self._pab}

    def get_bucket_policy_status(self, Bucket):  # noqa: N803 - boto3 kwarg casing
        self.calls.append(("get_bucket_policy_status", Bucket))
        if isinstance(self._policy, Exception):
            raise self._policy
        return {"PolicyStatus": self._policy}


def private_bucket_as_deployed():
    """The shape of the real dev bucket: all four flags true, no policy."""
    return StubS3(
        public_access_block=all_flags(True),
        policy_status=client_error("NoSuchBucketPolicy", "GetBucketPolicyStatus"),
    )


# --------------------------------------------------------------------------
# the deployed shape -- the case that fails today
# --------------------------------------------------------------------------


def test_all_flags_true_and_no_bucket_policy_passes():
    """THE defect. This is the deployed dev bucket, and it is private:
    ``NoSuchBucketPolicy`` means there is no policy, therefore no
    policy-granted public access. The replaced one-liner returned exit 254
    here."""
    s3 = private_bucket_as_deployed()

    state = assert_bucket_is_private(s3, BUCKET)

    assert state["hasBucketPolicy"] is False
    assert state["publicAccessBlock"] == all_flags(True)
    # Both legs must actually have run: a pass produced by skipping the policy
    # check is not the property this asserts.
    assert [call[0] for call in s3.calls] == [
        "get_public_access_block",
        "get_bucket_policy_status",
    ]


def test_the_deployed_shape_exits_zero_through_main(capsys):
    """End to end at the exit-code level, which is what the smoke test reads."""
    exit_code = main(["--bucket", BUCKET], s3_client=private_bucket_as_deployed())

    assert exit_code == 0
    out = capsys.readouterr().out
    assert BUCKET in out
    assert "no bucket policy" in out


# --------------------------------------------------------------------------
# a non-public policy is also a pass
# --------------------------------------------------------------------------


def test_all_flags_true_and_a_non_public_policy_passes():
    """A bucket that does have a policy stays supported: ``IsPublic=false`` is
    the other pass, and the state distinguishes it from "no policy" so the log
    says which one was observed."""
    s3 = StubS3(
        public_access_block=all_flags(True), policy_status={"IsPublic": False}
    )

    state = assert_bucket_is_private(s3, BUCKET)

    assert state["hasBucketPolicy"] is True


def test_a_non_public_policy_exits_zero_through_main(capsys):
    s3 = StubS3(public_access_block=all_flags(True), policy_status={"IsPublic": False})

    assert main(["--bucket", BUCKET], s3_client=s3) == 0
    assert "a non-public bucket policy" in capsys.readouterr().out


# --------------------------------------------------------------------------
# the check can go RED -- the anti-vacuity assertions
# --------------------------------------------------------------------------


def test_a_public_bucket_policy_fails():
    """Proves the check is capable of failing. Without this the suite could be
    green against an implementation that returns 0 unconditionally, which is
    exactly the vacuous gate #4517 hit."""
    s3 = StubS3(public_access_block=all_flags(True), policy_status={"IsPublic": True})

    with pytest.raises(BucketPrivacyError) as excinfo:
        assert_bucket_is_private(s3, BUCKET)

    assert "PUBLIC" in str(excinfo.value)


def test_a_public_bucket_policy_exits_one_through_main(capsys):
    """The failing path must reach exit 1 and annotate, not raise out of main."""
    s3 = StubS3(public_access_block=all_flags(True), policy_status={"IsPublic": True})

    assert main(["--bucket", BUCKET], s3_client=s3) == 1

    captured = capsys.readouterr()
    assert "::error" in captured.err
    assert captured.out == "", "a failing check must not also print a success line"


def test_all_four_flags_are_required():
    """Narrowing the predicate must fail here rather than quietly shrinking the
    parametrised test below. ``RestrictPublicBuckets`` is the one that matters
    most: it is what makes an attached public policy inert, so a predicate
    without it would read a bucket with a public policy as private."""
    assert set(REQUIRED_PUBLIC_ACCESS_BLOCK_FLAGS) == set(EXPECTED_FLAGS)


@pytest.mark.parametrize("unset_flag", sorted(EXPECTED_FLAGS))
def test_any_single_flag_not_true_fails(unset_flag):
    """Each flag is checked individually, not merely the configuration's
    presence. Parametrised over all four so that dropping any one of them from
    the predicate fails this gate -- notably ``RestrictPublicBuckets``, the flag
    that makes an attached public policy inert and therefore the one whose
    absence is easiest to overlook."""
    flags = all_flags(True)
    flags[unset_flag] = False
    s3 = StubS3(public_access_block=flags, policy_status={"IsPublic": False})

    with pytest.raises(BucketPrivacyError) as excinfo:
        assert_bucket_is_private(s3, BUCKET)

    assert unset_flag in str(excinfo.value)
    # Short-circuits: the governing control failed, so the policy leg is moot.
    assert ("get_bucket_policy_status", BUCKET) not in s3.calls


def test_a_missing_flag_is_treated_as_not_true():
    """A flag absent from the response is unknown, and unknown is not true. A
    ``config.get(flag)`` truthiness test would read ``None`` as a fail already,
    but an ``is False`` comparison would not -- hence the explicit case."""
    flags = all_flags(True)
    del flags["RestrictPublicBuckets"]
    s3 = StubS3(public_access_block=flags, policy_status={"IsPublic": False})

    with pytest.raises(BucketPrivacyError) as excinfo:
        assert_public_access_block(s3, BUCKET)

    assert "RestrictPublicBuckets" in str(excinfo.value)


# --------------------------------------------------------------------------
# absent public-access-block: the opposite of a pass
# --------------------------------------------------------------------------


def test_absent_public_access_block_configuration_fails():
    """``NoSuchPublicAccessBlockConfiguration`` means nothing is blocking public
    access. It is the case a naive "no error means fine" implementation gets
    backwards, and it is the regression shape if
    platform/infra/modules/security-scans/main.tf ever loses the block."""
    s3 = StubS3(
        public_access_block=client_error(
            "NoSuchPublicAccessBlockConfiguration", "GetPublicAccessBlock"
        )
    )

    with pytest.raises(BucketPrivacyError) as excinfo:
        assert_bucket_is_private(s3, BUCKET)

    assert "NoSuchPublicAccessBlockConfiguration" in str(excinfo.value)


# --------------------------------------------------------------------------
# errors are never swallowed
# --------------------------------------------------------------------------


def test_access_denied_on_the_public_access_block_fails():
    """The #4517 failure shape. An unreadable control leaves privacy unproven,
    and unproven is not private."""
    s3 = StubS3(
        public_access_block=client_error("AccessDenied", "GetPublicAccessBlock")
    )

    with pytest.raises(BucketPrivacyError) as excinfo:
        assert_bucket_is_private(s3, BUCKET)

    message = str(excinfo.value)
    assert "AccessDenied" in message
    # The cause must be operator-actionable: which grant is missing.
    assert "s3:GetBucketPublicAccessBlock" in message


def test_access_denied_on_the_policy_status_fails():
    """``AccessDenied`` must NOT be conflated with ``NoSuchBucketPolicy``. Both
    are errors from the same call, and treating the class rather than the code
    as the pass is the bug that would make this gate vacuous."""
    s3 = StubS3(
        public_access_block=all_flags(True),
        policy_status=client_error("AccessDenied", "GetBucketPolicyStatus"),
    )

    with pytest.raises(BucketPrivacyError) as excinfo:
        assert_bucket_is_private(s3, BUCKET)

    message = str(excinfo.value)
    assert "AccessDenied" in message
    assert "s3:GetBucketPolicyStatus" in message


@pytest.mark.parametrize(
    "code",
    [
        "SlowDown",
        "ThrottlingException",
        "NoSuchBucket",
        "InternalError",
    ],
)
def test_other_policy_status_errors_fail(code):
    """Throttling, a missing bucket and a server error all leave the policy's
    public-ness unknown. Only ``NoSuchBucketPolicy`` is a pass."""
    s3 = StubS3(
        public_access_block=all_flags(True),
        policy_status=client_error(code, "GetBucketPolicyStatus"),
    )

    with pytest.raises(BucketPrivacyError):
        assert_bucket_is_private(s3, BUCKET)


def test_a_non_client_error_is_not_mistaken_for_a_pass():
    """A connection error has no ``response`` dict. Reading the error code
    defensively means such an exception cannot be matched against
    ``NoSuchBucketPolicy`` and slip through as private."""
    s3 = StubS3(
        public_access_block=all_flags(True),
        policy_status=OSError("connection reset"),
    )

    with pytest.raises(BucketPrivacyError) as excinfo:
        assert_bucket_policy_not_public(s3, BUCKET)

    assert "OSError" in str(excinfo.value)


def test_every_failure_path_reaches_exit_one_through_main():
    """Each raising case must surface as exit 1 rather than a traceback: a
    traceback in a workflow step is still a failure, but an unhandled one is
    not the named, actionable cause this script exists to print."""
    failing = [
        StubS3(public_access_block=client_error("AccessDenied", "GetPublicAccessBlock")),
        StubS3(
            public_access_block=client_error(
                "NoSuchPublicAccessBlockConfiguration", "GetPublicAccessBlock"
            )
        ),
        StubS3(public_access_block=all_flags(False), policy_status={"IsPublic": False}),
        StubS3(public_access_block=all_flags(True), policy_status={"IsPublic": True}),
        StubS3(
            public_access_block=all_flags(True),
            policy_status=client_error("AccessDenied", "GetBucketPolicyStatus"),
        ),
    ]
    for s3 in failing:
        assert main(["--bucket", BUCKET], s3_client=s3) == 1


# --------------------------------------------------------------------------
# no blanket-pass constructs in the source
# --------------------------------------------------------------------------


def test_the_script_swallows_nothing_in_an_except_handler():
    """Behavioural tests cover today's code; this covers tomorrow's edit.

    A bare ``except: pass``, or a computed success value returned from an except
    handler, would make every test above pass while the gate went green on a
    public bucket. Asserted on the source because the whole point is to catch
    the construct being *introduced*.
    """
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
        body = [n for n in handler.body if not isinstance(n, ast.Pass)]
        assert body, "an except handler that only passes swallows the error"
        for node in ast.walk(handler):
            if isinstance(node, ast.Return):
                # `return False` in the NoSuchBucketPolicy branch is the one
                # legitimate early return: it means "no policy", and the caller
                # still treats it as a pass only after the flags leg succeeded.
                assert isinstance(node.value, ast.Constant), (
                    "an except handler must not return a computed success value"
                )


def test_only_no_such_bucket_policy_is_named_as_a_passable_error():
    """Guards the one deliberate exemption from widening. If a second error
    code is ever added to the pass branch, this fails and the widening has to
    be argued for in review rather than slipped in."""
    source = SCRIPT_PATH.read_text(encoding="utf-8")
    for code in ("AccessDenied", "SlowDown", "NoSuchBucket"):
        assert f'== "{code}"' not in source, (
            f"{code} appears to be compared as a passable error code; only "
            "NoSuchBucketPolicy may be treated as a pass"
        )


# --------------------------------------------------------------------------
# CLI contract
# --------------------------------------------------------------------------


def test_bucket_is_required():
    """The bucket is passed in from the workflow env rather than rebuilt here,
    so there is deliberately no default to fall back to."""
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_boto3_is_not_imported_at_module_scope():
    """Imported late, matching code_review_request.py, so ``--help`` and this
    suite work on an interpreter that has not been provisioned yet."""
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert all(alias.name != "boto3" for alias in node.names)
        assert not (isinstance(node, ast.ImportFrom) and node.module == "boto3")


# --------------------------------------------------------------------------
# the assertion is actually wired into the nightly, and CI actually runs this
# --------------------------------------------------------------------------


def test_the_nightly_runs_the_assertion_after_publishing():
    """An assertion no workflow invokes verifies nothing. It must run after the
    publish step -- the point is to prove the destination the findings just
    landed in is private -- and carry `if: always()` like publish does, so it
    still runs on the paths where publish did."""
    yaml = pytest.importorskip("yaml", reason="PyYAML required to parse the workflow")
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["code-review"]["steps"]

    publish_index = next(
        i
        for i, step in enumerate(steps)
        if "publish-findings-s3" in (step.get("uses") or "")
    )
    assert_indexes = [
        i
        for i, step in enumerate(steps)
        if "assert_findings_bucket_private.py" in (step.get("run") or "")
    ]
    assert len(assert_indexes) == 1, (
        "the code-review job must assert the findings bucket's privacy exactly once"
    )
    assert assert_indexes[0] > publish_index, (
        "the privacy assertion must follow the publish step it verifies"
    )
    step = steps[assert_indexes[0]]
    assert step.get("if") == "always()", (
        "publish is if: always(), so an assertion without it would silently skip "
        "on exactly the runs that published under a failure"
    )
    # The script's exit code must be able to fail the job. `|| true` or
    # continue-on-error would move the vacuous gate out of the script and into
    # the workflow, where the script's own tests could not see it.
    assert "|| true" not in step["run"]
    assert step.get("continue-on-error") is not True, (
        "a privacy assertion the job ignores is not a gate"
    )


def test_the_nightly_step_fails_loudly_on_an_unresolved_account_id():
    """``if: always()`` also reaches runs where "Resolve account ID" never ran.

    With ACCOUNT_ID empty the bucket name resolves to a trailing-dash string,
    and the assertion would report NoSuchBucket -- blaming the bucket for an
    upstream failure. It must still be a failure (privacy really is unverified),
    but with a cause that points at the right step.
    """
    yaml = pytest.importorskip("yaml", reason="PyYAML required to parse the workflow")
    workflow = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))
    body = next(
        step["run"]
        for step in workflow["jobs"]["code-review"]["steps"]
        if "assert_findings_bucket_private.py" in (step.get("run") or "")
    )
    assert '-z "${ACCOUNT_ID}"' in body, (
        "the step must name an empty ACCOUNT_ID rather than pass it through"
    )
    assert "exit 1" in body, "an unresolvable bucket name is not a pass"


def test_the_nightly_reuses_the_publish_steps_bucket_expression():
    """A second, independently written bucket name is a way for the assertion to
    certify a bucket the findings did not land in."""
    yaml = pytest.importorskip("yaml", reason="PyYAML required to parse the workflow")
    steps = yaml.safe_load(NIGHTLY_WORKFLOW.read_text(encoding="utf-8"))["jobs"][
        "code-review"
    ]["steps"]

    publish_bucket = next(
        step["with"]["bucket"]
        for step in steps
        if "publish-findings-s3" in (step.get("uses") or "")
    )
    assert_body = next(
        step["run"]
        for step in steps
        if "assert_findings_bucket_private.py" in (step.get("run") or "")
    )

    # Same env vars, same shape -- compared after normalising the two syntaxes
    # GitHub offers for reading them (`${{ env.X }}` in a `with:`, `${X}` in a
    # shell body).
    normalised = publish_bucket.replace("${{ env.", "${").replace(" }}", "}")
    assert normalised in assert_body, (
        f"the assertion checks a bucket named differently from the publish "
        f"target ({publish_bucket!r}); the two must not be able to drift"
    )


def test_the_nightly_no_longer_cites_the_broken_smoke_command():
    """The stale comment naming ``get-bucket-policy-status`` as the confirming
    command is the origin of this defect; left in place it re-teaches the wrong
    command to the next reader."""
    # Comment prose wraps, so the sentence is asserted against whitespace- and
    # comment-marker-normalised text rather than raw lines.
    prose = " ".join(NIGHTLY_WORKFLOW.read_text(encoding="utf-8").split())
    prose = prose.replace("# ", "")

    assert "confirms with `aws s3api get-bucket-policy-status`" not in prose, (
        "the nightly must not name get-bucket-policy-status as the command that "
        "confirms the findings bucket is private -- it errors NoSuchBucketPolicy "
        "on that bucket by design (#4533)"
    )
    # The replacement must be named in its place, so a reader of this file is
    # pointed at the assertion that does work rather than left with none.
    assert "assert_findings_bucket_private.py" in prose


def test_the_new_suite_is_pinned_in_the_script_tests_gate():
    """script-tests.yml pins its suite list explicitly rather than globbing (a
    glob silently stops covering a renamed file), so a new suite must be added
    there by hand or CI never runs it and this gate is vacuous."""
    workflow = SCRIPT_TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "tests/test_assert_findings_bucket_private.py" in workflow, (
        "this suite is not pinned in script-tests.yml, so CI does not run it"
    )
    assert ".github/scripts/assert_findings_bucket_private.py" in workflow, (
        "the subject script is not in the paths triggers, so editing it alone "
        "would not run this suite"
    )


def test_the_scan_identity_can_make_both_calls():
    """Privacy checks use the protected scan dispatcher, not ambient ARC IAM."""
    tf = SCAN_IAM_TF.read_text(encoding="utf-8")
    for action in REQUIRED_RUNNER_ACTIONS:
        assert f'"{action}"' in tf, f"The scan identity is missing {action}"
    import yaml
    job = yaml.safe_load(NIGHTLY_WORKFLOW.read_text())["jobs"]["code-review"]
    assert str(job["environment"]).startswith("adp-scan-")
    assert any(step.get("uses") == "aws-e/adp/.github/actions/trusted-scan@main" for step in job["steps"])
