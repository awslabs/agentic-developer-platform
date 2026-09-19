"""Ownership decides on the module's REAL names — Issue #5042 (U3), EPIC #4910.

## The reproduction these tests lock down

A checkpoint review of `84e3f7ee` ran the published guard against offline plan fixtures and
found it inverted on the cases that matter most:

| Case                                                          | Required | Was |
|---------------------------------------------------------------|---------:|----:|
| Create the real IAM role `adp-dev-superplane-control-plane`    |        0 |   1 |
| Create the real SSM parameter `/adp/dev/superplane/namespace`  |        0 |   1 |
| Create the foreign `gateway-adp-superplane-api`                | non-zero |   0 |
| Plan `{"resource_changes": false}`                             | non-zero |   0 |

Two independent defects produced that:

1.  The guard asserted a flat `adp-superplane-` prefix and an SSM prefix `/adp/superplane/`.
    The module builds `adp-${var.environment}-superplane` (`main.tf:69`) and
    `/adp/${var.environment}/superplane` (`config.tf:23`). So it denied its own resources,
    including on the first legitimate apply — a guard that makes the lane permanently
    unusable, which the review explicitly called out as the wrong kind of "safe".
2.  `_value_is_domain_owned` tested `DOMAIN_PREFIX in value` — unanchored — so
    `gateway-adp-superplane-api` passed by containing the prefix somewhere.

The existing suite could not catch either, because its positive fixtures were hand-written
names (`adp-superplane-dev-api`) that no Terraform here produces. Fixture and guard shared
one wrong assumption, so they agreed. Every positive case below is therefore DERIVED from the
Terraform source via `source_derived_names.py`.

## The coverage shape the review required

"Regression coverage must include source-derived valid creation, refresh/no-op/update and
destroy identities as well as hostile names/accounts/environments, so safety does not make
the lane permanently unusable." Both directions are represented below: the positive half
proves the lane can run, the negative half proves it is still a guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_derived_names as names  # noqa: E402
from domain_ownership import (  # noqa: E402
    OwnershipError,
    validate_identity,
    validate_plan,
)

ACCOUNT = "879318057152"
REGION = "us-east-1"
ENVIRONMENT = "dev"


def _plan(*changes: dict) -> dict:
    return {"format_version": "1.2", "resource_changes": list(changes)}


def _change(address: str, actions: list[str], before=None, after=None) -> dict:
    return {
        "address": address,
        "change": {"actions": actions, "before": before, "after": after},
    }


def _validate(*changes: dict):
    return validate_plan(_plan(*changes), account_id=ACCOUNT, environment=ENVIRONMENT)


# ---------------------------------------------------------------------------
# Source-derived POSITIVE cases — the half whose absence caused the defect.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_name", names.iam_role_names(ENVIRONMENT))
def test_real_iam_role_creation_is_owned(role_name):
    """Every `aws_iam_role` the module declares must be creatable.

    The first of the review's four inverted cases: this returned exit 1 before the fix.
    """
    report = _validate(
        _change(
            "aws_iam_role.control_plane",
            ["create"],
            after={
                "name": role_name,
                "arn": names.iam_role_arn(ENVIRONMENT, ACCOUNT, role_name),
            },
        )
    )
    assert report.ok, f"the module's own IAM role was denied: {
        [str(v) for v in report.violations]
    }"


@pytest.mark.parametrize("parameter_name", names.ssm_parameter_names(ENVIRONMENT))
def test_real_ssm_parameter_creation_is_owned(parameter_name):
    """Every `aws_ssm_parameter` the module declares must be creatable.

    All eleven are checked rather than one, because the path prefix is shared but the
    suffixes are not, and a rule keyed on a suffix would pass a sample and fail the rest.
    """
    report = _validate(
        _change(
            "aws_ssm_parameter.config",
            ["create"],
            after={
                "name": parameter_name,
                "arn": names.ssm_parameter_arn(
                    ENVIRONMENT, ACCOUNT, REGION, parameter_name
                ),
            },
        )
    )
    assert report.ok, f"the module's own SSM parameter was denied: {
        [str(v) for v in report.violations]
    }"


@pytest.mark.parametrize("repository", names.ecr_repository_names())
def test_real_ecr_repository_creation_is_owned(repository):
    """Repository names come from U2's lock and carry NO environment segment by design."""
    report = _validate(
        _change(
            f'aws_ecr_repository.superplane["{repository}"]',
            ["create"],
            after={
                "name": repository,
                "arn": names.ecr_repository_arn(ACCOUNT, REGION, repository),
            },
        )
    )
    assert report.ok, f"a locked ECR repository was denied: {
        [str(v) for v in report.violations]
    }"


def test_ecr_repository_is_environment_independent_in_every_environment():
    """The same repository name must validate under any environment.

    This is the property that makes ECR's exemption a design decision rather than a hole: if
    it only passed for `dev`, the lane would break the first time it ran elsewhere.
    """
    repository = names.ecr_repository_names()[0]
    for environment in ("dev", "staging", "prod", "embark1"):
        report = validate_plan(
            _plan(
                _change(
                    f'aws_ecr_repository.superplane["{repository}"]',
                    ["create"],
                    after={"name": repository},
                )
            ),
            account_id=ACCOUNT,
            environment=environment,
        )
        assert report.ok, f"{repository} was denied in environment {environment}"


@pytest.mark.parametrize("actions", [["no-op"], ["update"], ["read"]])
def test_refresh_noop_and_update_identities_are_owned(actions):
    """A refresh/no-op/update carries equal before and after; both must validate.

    Named explicitly by the review. A guard that only understood create and delete would
    reject an ordinary `terraform apply` that changed a tag.
    """
    role_name = names.iam_role_names(ENVIRONMENT)[0]
    values = {
        "name": role_name,
        "arn": names.iam_role_arn(ENVIRONMENT, ACCOUNT, role_name),
    }
    report = _validate(
        _change("aws_iam_role.control_plane", actions, before=values, after=values)
    )
    assert report.ok, [str(v) for v in report.violations]


def test_real_destroy_identity_is_owned_and_flagged_destructive():
    """A destroy of a real resource is permitted AND reported as destructive."""
    role_name = names.iam_role_names(ENVIRONMENT)[0]
    report = _validate(
        _change(
            "aws_iam_role.control_plane",
            ["delete"],
            before={
                "name": role_name,
                "arn": names.iam_role_arn(ENVIRONMENT, ACCOUNT, role_name),
            },
        )
    )
    assert report.ok, [str(v) for v in report.violations]
    assert report.has_destructive_changes


def test_managed_policy_attachment_to_domain_role_is_owned():
    """`policy_arn` is an EXTERNAL reference and must not require domain naming.

    `irsa.tf:186` attaches `var.skypilot_compute_policy_arns` — AWS-managed ARNs such as
    `arn:aws:iam::aws:policy/AmazonEC2FullAccess`, whose ARN account field is the literal
    `aws`. A generic "must carry the domain prefix and the selected account" rule would deny
    the SkyPilot compute grant the moment one was authorized. The `role` half is still
    checked, which is what stops attaching a policy to somebody else's role.
    """
    report = _validate(
        _change(
            'aws_iam_role_policy_attachment.skypilot_compute["ec2"]',
            ["create"],
            after={
                "role": names.iam_role_names(ENVIRONMENT)[1],
                "policy_arn": "arn:aws:iam::aws:policy/AmazonEC2FullAccess",
            },
        )
    )
    assert report.ok, [str(v) for v in report.violations]


def test_managed_policy_attached_to_foreign_role_is_denied():
    report = _validate(
        _change(
            "aws_iam_role_policy_attachment.foreign",
            ["create"],
            after={
                "role": "bedrockgw-dev-role",
                "policy_arn": "arn:aws:iam::aws:policy/AmazonEC2FullAccess",
            },
        )
    )
    assert not report.ok, "a policy attached to the gateway's role was accepted"


# ---------------------------------------------------------------------------
# Hostile names, accounts and environments.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_name",
    [
        # The review's exact case: contains the domain prefix, is not ours.
        "gateway-adp-superplane-api",
        "bedrockgw-adp-superplane-api",
        "adp-superplane-api-gateway",
        "x-adp-dev-superplane-control-plane",
        "notadp-dev-superplane-control-plane",
    ],
)
def test_names_merely_containing_the_prefix_are_denied(hostile_name):
    """Anchoring, not substring. Every name here contains a domain-looking fragment."""
    report = _validate(
        _change("aws_ecr_repository.hostile", ["create"], after={"name": hostile_name})
    )
    assert not report.ok, f"{hostile_name!r} was accepted as domain-owned"


@pytest.mark.parametrize("other_environment", ["prod", "staging", "embark1"])
def test_other_environments_flat_names_are_denied(other_environment):
    """A dev run must not touch another environment's environment-scoped resources.

    Bound POSITIVELY: the name must match the SELECTED environment. The previous rule
    excluded a list of other environment words, which passed any environment token nobody
    had enumerated.
    """
    foreign = names.iam_role_names(other_environment)[0]
    report = _validate(
        _change("aws_iam_role.control_plane", ["delete"], before={"name": foreign})
    )
    assert not report.ok, f"a dev run accepted {foreign!r}"


def test_unenumerated_environment_token_is_denied():
    """The case the exclusion-list approach could not catch.

    `adp-sandbox7-superplane-control-plane` names an environment that is not in
    KNOWN_ENVIRONMENTS. Excluding dev/staging/prod/embark1 would have passed it; requiring
    the selected environment rejects it.
    """
    report = _validate(
        _change(
            "aws_iam_role.control_plane",
            ["delete"],
            before={"name": "adp-sandbox7-superplane-control-plane"},
        )
    )
    assert not report.ok, "an unenumerated environment token was accepted"


def test_ssm_path_of_another_environment_is_denied():
    foreign = names.ssm_parameter_names("prod")[0]
    report = _validate(
        _change("aws_ssm_parameter.config", ["delete"], before={"name": foreign})
    )
    assert not report.ok, f"a dev run accepted {foreign!r}"


def test_ecr_name_embedding_an_environment_is_denied():
    """`adp-superplane-dev-api` contradicts the environment-independent design."""
    report = _validate(
        _change(
            "aws_ecr_repository.superplane",
            ["create"],
            after={"name": "adp-superplane-dev-api"},
        )
    )
    assert not report.ok, "an environment-scoped repository name was accepted"


def test_foreign_account_in_arn_field_is_denied():
    """Account is compared as an ARN FIELD, not as a substring of the whole value."""
    role_name = names.iam_role_names(ENVIRONMENT)[0]
    report = _validate(
        _change(
            "aws_iam_role.control_plane",
            ["create"],
            after={
                "name": role_name,
                "arn": f"arn:aws:iam::605440105851:role/{role_name}",
            },
        )
    )
    assert not report.ok, "an ARN naming upstream's account was accepted"


def test_account_id_appearing_inside_a_name_does_not_satisfy_the_account_check():
    """A name containing the selected account digits is not an account match.

    Field-wise ARN parsing is what makes this distinguishable; a substring search for the
    account id would have been satisfied by the resource name.
    """
    report = _validate(
        _change(
            "aws_iam_role.control_plane",
            ["create"],
            after={
                "name": f"adp-{ENVIRONMENT}-superplane-{ACCOUNT}",
                "arn": f"arn:aws:iam::605440105851:role/adp-{ENVIRONMENT}-superplane-x",
            },
        )
    )
    assert not report.ok


def test_wrong_service_arn_is_denied():
    """An `aws_ecr_repository` whose ARN names IAM is not a repository."""
    repository = names.ecr_repository_names()[0]
    report = _validate(
        _change(
            "aws_ecr_repository.superplane",
            ["create"],
            after={
                "name": repository,
                "arn": f"arn:aws:iam::{ACCOUNT}:role/{repository}",
            },
        )
    )
    assert not report.ok, "an IAM ARN satisfied an ECR field"


def test_missing_environment_refuses_rather_than_skipping_the_check():
    """Omitting `--environment` must not silently disable environment binding.

    Otherwise the most dangerous invocation — one that forgot to say which environment it
    targeted — would be the least constrained.
    """
    role_name = names.iam_role_names(ENVIRONMENT)[0]
    with pytest.raises(OwnershipError):
        validate_plan(
            _plan(
                _change(
                    "aws_iam_role.control_plane", ["create"], after={"name": role_name}
                )
            ),
            account_id=ACCOUNT,
        )


@pytest.mark.parametrize("hostile", ["dev|prod", "dev.*", "../prod", "DEV", "d" * 40])
def test_hostile_environment_token_is_rejected(hostile):
    """The environment is interpolated into a regex, so it must be validated first."""
    with pytest.raises(OwnershipError):
        validate_identity(
            "aws_iam_role.control_plane",
            None,
            {"name": "adp-dev-superplane-control-plane"},
            account_id=ACCOUNT,
            environment=hostile,
        )


def test_allowlisted_type_without_an_identity_rule_is_denied():
    """`terraform_data` is allowlisted and exempt; anything else must have a rule.

    Guards against a future widening of ALLOWED_RESOURCE_TYPES that forgets IDENTITY_RULES,
    which would otherwise fall through to "no rule, therefore fine".
    """
    import domain_ownership as ownership

    assert set(ownership.ALLOWED_RESOURCE_TYPES) - {"terraform_data"} <= set(
        ownership.IDENTITY_RULES
    ), "an allowlisted type has no identity rule, so its ownership cannot be decided"


# ---------------------------------------------------------------------------
# Plan SHAPE: malformed structures must not normalize to "empty".
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed", [False, True, 0, 1, "", "changes", {}, {"a": 1}, 0.0]
)
def test_malformed_resource_changes_is_refused(malformed):
    """The review's fourth inverted case.

    `changes = plan.get("resource_changes") or []` coerced every falsy wrong type into an
    accepted empty list BEFORE the type check, so `{"resource_changes": false}` printed
    `Validated 0 resource change(s)` and exited 0.
    """
    with pytest.raises(OwnershipError):
        validate_plan(
            {"format_version": "1.2", "resource_changes": malformed},
            environment=ENVIRONMENT,
        )


@pytest.mark.parametrize(
    "plan",
    [
        {"format_version": "1.2"},
        {"format_version": "1.2", "resource_changes": []},
    ],
)
def test_legitimate_empty_plan_is_still_accepted(plan):
    """A real empty plan must keep working — the second `apply` in R3 acc. 2 produces one."""
    report = validate_plan(plan, account_id=ACCOUNT, environment=ENVIRONMENT)
    assert report.ok and report.checked == 0
    assert not report.has_destructive_changes


@pytest.mark.parametrize(
    "actions",
    [["destroy"], ["remove"], ["DELETE"], ["create", "destroy"], [""], ["no_op"]],
)
def test_invalid_action_vocabulary_is_refused(actions):
    """An unrecognised action must deny, not fail to match the destructive set.

    `["destroy"]` is the dangerous shape: it is not Terraform's spelling (`delete` is), so a
    membership test against {"delete"} silently classifies it as safe.
    """
    with pytest.raises(OwnershipError):
        validate_plan(
            _plan(_change("aws_iam_role.control_plane", actions)),
            environment=ENVIRONMENT,
        )


@pytest.mark.parametrize(
    "actions", [["delete"], ["delete", "create"], ["create", "delete"]]
)
def test_both_replacement_orderings_count_as_destructive(actions):
    """Replacement is a delete. Both lifecycle orderings must be flagged."""
    role_name = names.iam_role_names(ENVIRONMENT)[0]
    values = {"name": role_name}
    report = _validate(
        _change("aws_iam_role.control_plane", actions, before=values, after=values)
    )
    assert report.ok, [str(v) for v in report.violations]
    assert report.has_destructive_changes, (
        f"{actions} was not classified as destructive"
    )
