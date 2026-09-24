"""The workspace plan guard fails closed — Issue #5532 (w6-09), design item 4, AC-01.

Design item 4: *"Produce saved plans, change inventories and bounded cost/resource estimates;
deny unexpected replacements/deletes or ownership changes without exact plan authorization."*

## What these tests execute, and why it matters that it is the real thing

Every test below runs `../scripts/check_workspace_plan.py` as a SUBPROCESS and asserts on its
exit code. Not the functions it imports — the script, with its argument parsing, its file
reading, and its exit-code mapping intact.

The reason is the defect this whole pattern descends from. PR #5283's review found a gate
exiting 0 on a plan that destroyed an ECR repository, and the bug was not in any function's
logic: it was in how the script interpreted a subprocess's combination of stdout and exit
code, and in a shell construct that made the failure path indistinguishable from the success
path. Calling `validate_plan` directly and asserting on the returned report would test the
part that was never broken.

So: exit 0 means approved, exit non-zero means denied, and each test states which it expects.

## The positive fixtures are DERIVED, not hand-written

`_planned_name()` and friends build every accepted name from
`workspace_ownership.name_prefix()`, which reproduces `main.tf`'s `local.name_prefix`. A
hand-written positive fixture cannot catch a naming disagreement between the guard and the
Terraform, because it IS the disagreement — a checkpoint review of `84e3f7ee` found exactly
that in the control plane's suite: the guard denied every resource the module creates while
the suite passed, because both encoded the same wrong assumption.

`test_the_guard_accepts_the_names_this_module_actually_creates` closes the remaining gap by
reading the real `.tf` source rather than the shared helper, so a prefix change in `main.tf`
that the helper tracked silently would still fail.

## What these fixtures are, and the one thing they cannot prove

The plan documents below are hand-built, because producing a real one requires
`terraform plan` against a live account — credentials this module's lane deliberately does not
have, and which AC-01 excludes by requiring the tests be provider-free.

That leaves one gap: a fixture could name an attribute the AWS provider does not actually
emit, and every test here would still pass while the guard read `None` from a real plan. The
gap was closed OUT OF BAND rather than left open — every attribute the guard's identity rules
read (`name`, `node_group_name`, `instance_types`, `scaling_config[].max_size`, `arn`,
`tags_all`) was checked against `terraform providers schema -json` for
`hashicorp/aws ~> 6.0`, and `scaling_config` was confirmed to be a LIST-nested block, which is
why the guard indexes `[0]`.

To redo that check after a provider upgrade: init a copy of this module with the `backend "s3"`
block removed (the dump needs no credentials, but it does need a resolvable backend), run
`terraform providers schema -json`, and confirm each attribute in `IDENTITY_FIELDS` and
`TAG_IDENTIFIED_TYPES` still exists. `test_every_allowed_type_has_an_identity_rule` and
`test_every_type_the_module_declares_is_allowed_and_identifiable` cover the other direction —
that the guard and the Terraform agree on which types exist — and those DO run offline.

## The anti-vacuous concern

A denial test passes if the guard denies for the RIGHT reason or the wrong one — a guard that
denied everything unconditionally would pass every negative test here. Two things guard
against that reading: every negative test asserts on the denial REASON appearing in output,
and the accept-path tests (`...is_approved`, `..._accepts_the_names_this_module_actually_
creates`) fail if the guard has become uniformly hostile.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WORKSPACES = Path(__file__).resolve().parents[1]
GUARD = WORKSPACES / "scripts" / "check_workspace_plan.py"

sys.path.insert(0, str(WORKSPACES / "scripts"))

from workspace_ownership import name_prefix as _name_prefix, validate_plan  # noqa: E402

# The authorization-document builder and the plan digest are imported from the guard rather
# than reimplemented here — see `_authorization_for` for why the fixtures must not carry their
# own copy of the format.
from check_workspace_plan import (  # noqa: E402
    EBS_GP3_MONTHLY_USD_PER_GIB,
    HOURS_PER_MONTH,
    INSTANCE_HOURLY_USD,
    build_authorization,
    destructive_identity,
    verify_target,
    _plan_digest,
)

# Versions, rates and a region DERIVED from the support policy rather than hardcoded, so a
# policy review that retires a version or corrects a published price cannot leave these fixtures
# pinned to something the guard now refuses — which is how `no_inherited_defaults.tftest.hcl`
# came to pin the retired 1.30. Every cost expectation below is computed from these.
from region_version_policy import (  # noqa: E402
    CONTROL_PLANE_HOURLY_USD,
    VERSION_SUPPORT,
    createable_versions,
)

STANDARD_VERSION = next(
    v for v in createable_versions() if VERSION_SUPPORT[v].tier == "standard"
)
EXTENDED_VERSION = next(
    v for v in createable_versions() if VERSION_SUPPORT[v].tier == "extended"
)
RETIRED_VERSION = next(
    v for v, s in sorted(VERSION_SUPPORT.items()) if s.tier == "retired"
)

ENVIRONMENT = "dev"
WORKSPACE = "tenant-alpha"
OTHER_WORKSPACE = "tenant-beta"
ACCOUNT = "111122223333"
OTHER_ACCOUNT = "999988887777"
ORG_ID = "test-org"


def name_prefix(environment, workspace):
    # Legacy fixture helpers use distinct test IDs equal to their labels; production never derives IDs from names.
    return _name_prefix(environment, workspace, org_id=ORG_ID, workspace_id=workspace)


PREFIX = name_prefix(ENVIRONMENT, WORKSPACE)
TEST_KMS_KEY = (
    f"arn:aws:kms:us-east-1:{ACCOUNT}:key/11111111-2222-3333-4444-555555555555"
)
REGION = "us-east-1"
OTHER_REGION = "eu-west-1"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def _cluster(actions: list[str], *, workspace: str = WORKSPACE, **overrides) -> dict:
    values = {
        "name": name_prefix(ENVIRONMENT, workspace),
        "arn": f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/{name_prefix(ENVIRONMENT, workspace)}",
        # `version` is present because a real plan carries it and the bounded estimate now
        # REQUIRES it: the control-plane rate depends on the version's EKS support tier
        # ($0.10/hour standard, $0.60 extended), so a fixture without one would exercise only
        # the refusal path and never the pricing (finding W9-05). STANDARD_VERSION rather than a
        # literal, so the support policy and these fixtures cannot drift.
        "version": STANDARD_VERSION,
    }
    values.update(overrides)
    before = values if actions != ["create"] else None
    after = values if actions != ["delete"] else None
    return {
        "address": "aws_eks_cluster.workspace",
        "change": {"actions": actions, "before": before, "after": after},
    }


def _vpc(
    actions: list[str],
    *,
    workspace: str = WORKSPACE,
    environment: str = ENVIRONMENT,
    ownership: str = "adp-created",
    **kw,
) -> dict:
    # `Environment` is present because main.tf's `local.common_tags` sets it and the provider's
    # default_tags applies it, so a real plan's `tags_all` always carries it. It is a separate
    # parameter from `workspace` because the two are independently wrong: review finding W9-03
    # reproduced a VPC tagged Workspace=tenant-alpha, Environment=prod being accepted by a plan
    # for environment=dev, since nothing compared the environment. A fixture that omitted the
    # tag could not express that case at all.
    tags = {
        "Workspace": workspace,
        "WorkspaceId": workspace,
        "OrgId": ORG_ID,
        "Environment": environment,
        "NetworkOwnership": ownership,
        "Project": "adp",
    }
    values = {"cidr_block": "10.64.0.0/16", "tags_all": tags}
    values.update(kw)
    before = values if actions != ["create"] else None
    after = values if actions != ["delete"] else None
    return {
        "address": "aws_vpc.workspace[0]",
        "change": {"actions": actions, "before": before, "after": after},
    }


def _node_group(
    actions: tuple[str, ...] = ("create",),
    *,
    instance_types: list[str] | None = None,
    max_size: int | None = 2,
) -> dict:
    scaling = [{"desired_size": 1, "min_size": 0, "max_size": max_size}]
    values = {
        "node_group_name": f"{PREFIX}-default",
        "cluster_name": PREFIX,
        "instance_types": instance_types
        if instance_types is not None
        else ["m6i.large"],
        "scaling_config": scaling,
        "launch_template": [{"id": "lt-test-root", "version": "1"}],
    }
    return {
        "address": "aws_eks_node_group.default",
        "change": {
            "actions": list(actions),
            "before": values if actions != ("create",) else None,
            "after": None if actions == ("delete",) else values,
        },
    }


def _launch_template(
    actions: tuple[str, ...] = ("create",), *, volume_size: int | None = 50
) -> dict:
    """The node launch template added for finding W9-02, as a real plan renders it.

    `block_device_mappings` is a list of blocks and `ebs` inside it is a single-element LIST,
    not an object — that is how the AWS provider represents a nested block, and the guard
    indexes `[0]` accordingly. A fixture using a bare dict here would let a guard that got the
    nesting wrong pass.
    """
    ebs: dict = {
        "volume_type": "gp3",
        "encrypted": "true",
        "delete_on_termination": True,
        "kms_key_id": TEST_KMS_KEY,
    }
    if volume_size is not None:
        ebs["volume_size"] = volume_size
    values = {
        "name": f"{PREFIX}-node",
        "id": "lt-test-root",
        "latest_version": 1,
        "block_device_mappings": [{"device_name": "/dev/xvda", "ebs": [ebs]}],
    }
    return {
        "address": "aws_launch_template.node",
        "change": {
            "actions": list(actions),
            "before": None if actions == ("create",) else dict(values),
            "after": None if actions == ("delete",) else values,
        },
    }


def _complete_eks_fixture(changes):
    """Include parents required by minimal cluster/node test cases, as saved plans do."""
    addresses = {c.get("address") for c in changes}
    if "aws_eks_cluster.workspace" not in addresses:
        return []
    added = []
    parents = {
        "aws_iam_role.cluster": {
            "name": f"{PREFIX}-cluster-role",
            "arn": f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-cluster-role",
        },
        "aws_security_group.cluster": {
            "name": f"{PREFIX}-cluster",
            "id": "sg-0cluster",
            "vpc_id": "vpc-0suppliedbyowner",
        },
    }
    if "aws_eks_node_group.default" in addresses:
        parents["aws_iam_role.node"] = {
            "name": f"{PREFIX}-node-role",
            "arn": f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-node-role",
        }
        if "aws_launch_template.node" not in addresses:
            template = _launch_template(("no-op",))
            template["change"]["before"] = dict(template["change"]["after"])
            changes.append(template)
    for address, values in parents.items():
        if address not in addresses:
            changes.append(
                {
                    "address": address,
                    "change": {
                        "actions": ["no-op"],
                        "before": dict(values),
                        "after": dict(values),
                    },
                }
            )
            added.append(address)
    # Full native-shaped fixtures already contain their parents and are completed below.
    if not added:
        return []
    for change in changes:
        detail = change.get("change", {})
        for side in ("before", "after"):
            values = detail.get(side)
            if not isinstance(values, dict):
                continue
            if change.get("address") == "aws_eks_cluster.workspace":
                values.setdefault(
                    "role_arn", f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-cluster-role"
                )
                values.setdefault(
                    "vpc_config",
                    [
                        {
                            "subnet_ids": ["subnet-supplied-a", "subnet-supplied-b"],
                            "security_group_ids": ["sg-0cluster"],
                        }
                    ],
                )
            elif change.get("address") == "aws_eks_node_group.default":
                values.setdefault(
                    "node_role_arn", f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-node-role"
                )
                values.setdefault(
                    "subnet_ids", ["subnet-supplied-a", "subnet-supplied-b"]
                )
    return added


def _plan(
    *changes: dict,
    drift: list[dict] | None = None,
    workspace: str = WORKSPACE,
    environment: str = ENVIRONMENT,
    account: str = ACCOUNT,
    region: str = REGION,
) -> dict:
    """A plan document shaped like `terraform show -json` output.

    `variables` is present because the guard establishes the plan's TARGET from the plan's own
    `variables` block rather than from the command line (W9-04): an authorization whose account and
    region came from the caller's flags was then checked against those same flags, so the
    comparison could not fail. A fixture without `variables` therefore cannot reach any
    authorization control — it is refused earlier, for carrying no evidence of its own target.

    `terraform_version` is present for the same reason `format_version` is: the guard compares the
    reviewed JSON against the artifact's own rendering field by field, and those two are part of
    the comparison.
    """
    changes = list(changes)
    if any(
        c.get("address") == "aws_eks_node_group.default" for c in changes
    ) and not any(c.get("address") == "aws_eks_cluster.workspace" for c in changes):
        changes.append(_cluster(["no-op"]))
    resulting_nodes = [
        c
        for c in changes
        if c.get("address") == "aws_eks_node_group.default"
        and c["change"]["actions"] != ["delete"]
    ]
    if resulting_nodes and not any(
        c.get("address") == "aws_launch_template.node" for c in changes
    ):
        changes.append(_launch_template(("no-op",)))
    for node in resulting_nodes:
        node["change"]["after"].setdefault(
            "launch_template", [{"id": "lt-test-root", "version": "1"}]
        )
    added_parents = _complete_eks_fixture(changes)
    plan: dict = {
        "format_version": "1.2",
        "terraform_version": "1.9.8",
        "variables": {
            "account_id": {"value": account},
            "kms_key_arn": {"value": TEST_KMS_KEY if resulting_nodes else ""},
            "aws_region": {"value": region},
            "environment": {"value": environment},
            "workspace_name": {"value": workspace},
            "workspace_id": {"value": workspace},
            "org_id": {"value": ORG_ID},
        },
        "planned_values": {
            "outputs": {
                "provisioning_principal_arn": {
                    "value": f"arn:aws:iam::{account}:user/operator"
                },
                "provisioning_caller_kms_requirements": {
                    "value": {"principal_arn": f"arn:aws:iam::{account}:user/operator"}
                },
            }
        },
        "resource_changes": list(changes),
    }
    if added_parents:
        plan["variables"].update(
            {
                "networking_mode": {"value": "supplied"},
                "supplied_vpc_id": {"value": "vpc-0suppliedbyowner"},
                "supplied_private_subnet_ids": {
                    "value": ["subnet-supplied-a", "subnet-supplied-b"]
                },
            }
        )
        plan["configuration"] = {
            "root_module": {
                "resources": [
                    {
                        "address": "aws_security_group.cluster",
                        "expressions": {"vpc_id": {"references": ["local.vpc_id"]}},
                    },
                    {
                        "address": "aws_eks_cluster.workspace",
                        "expressions": {
                            "vpc_config": [
                                {
                                    "subnet_ids": {
                                        "references": ["local.private_subnet_ids"]
                                    }
                                }
                            ]
                        },
                    },
                    {
                        "address": "aws_eks_node_group.default",
                        "expressions": {
                            "subnet_ids": {"references": ["local.private_subnet_ids"]}
                        },
                    },
                ]
            }
        }
    if drift is not None:
        plan["resource_drift"] = drift
    return plan


def _write(tmp_path: Path, plan: dict, name: str = "plan.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def _stub_artifact(tmp_path: Path, plan_path: Path, name: str = "plan.tfplan") -> Path:
    """A stand-in for `terraform plan -out`: a real zip carrying a `tfplan` member.

    ## Why a double is the right thing here, and what it does NOT prove

    The guard requires a saved plan artifact for anything that authorizes, and verifies it three
    ways: the file is a zip containing a `tfplan` member, `terraform show -json` re-derives it, and
    its digest is bound. A real artifact can only be produced by the Terraform binary, and the lane
    that runs this suite (`superplane-domain-ci.yml`) has NO `setup-terraform` step — so making
    every authorization control depend on a real one would not make them stricter, it would make
    them unrunnable in the lane that actually runs them.

    So this builds a zip whose `tfplan` member is the plan JSON itself, paired with
    `_stub_terraform` below, which re-derives that member. Between them the guard's artifact code
    path executes for real: the zip shape is checked by the real `zipfile`, the derivation runs as a
    real subprocess, and the digest is a real SHA-256 of these bytes.

    What it does not prove is that a genuine Terraform saved plan satisfies the same checks — a
    double can only confirm the guard accepts the shape this file believes in. That is exactly the
    gap `test_a_real_terraform_saved_plan_is_accepted_end_to_end` closes, using a plan produced by
    the Terraform binary; it skips when Terraform is absent, which is why it is a supplement to
    these controls and not a replacement for them.
    """
    import zipfile

    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tfplan", plan_path.read_bytes())
    return path


def _stub_terraform(tmp_path: Path) -> Path:
    """A `terraform` stand-in whose `show -json` prints the artifact's own `tfplan` member.

    Deliberately not a mock that returns a fixed document: it reads the zip it is given, so the
    guard's "does this JSON derive from this artifact?" check is a real comparison between two
    independently-obtained values. Substituting one artifact for another therefore fails here just
    as it would with the real binary, which is what
    `test_an_authorization_bound_to_another_artifact_is_refused` relies on.
    """
    path = tmp_path / "terraform-stub"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, zipfile\n"
        "# argv is: show -json <artifact>. Anything else is not what the guard should be\n"
        "# calling, so fail loudly rather than silently satisfying the check.\n"
        "if sys.argv[1:3] != ['show', '-json']:\n"
        "    sys.exit('stub terraform called as %r' % (sys.argv[1:],))\n"
        "with zipfile.ZipFile(sys.argv[3]) as archive:\n"
        "    sys.stdout.write(archive.read('tfplan').decode())\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _run(
    plan_path: Path,
    *extra: str,
    workspace: str = WORKSPACE,
    environment: str = ENVIRONMENT,
    account: str | None = ACCOUNT,
    region: str | None = REGION,
    complete: bool = True,
) -> subprocess.CompletedProcess:
    command = [
        sys.executable,
        str(GUARD),
        "--plan-json",
        str(plan_path),
        "--environment",
        environment,
        "--workspace-name",
        workspace,
        "--workspace-id",
        workspace,
        "--org-id",
        ORG_ID,
    ]
    if account:
        command += ["--account-id", account]
    if region:
        command += ["--aws-region", region]
    if complete and plan_path.exists():
        if "--plan-file" not in extra:
            command += [
                "--plan-file",
                str(_stub_artifact(plan_path.parent, plan_path, name="default.tfplan")),
            ]
        if "--terraform-binary" not in extra:
            command += ["--terraform-binary", str(_stub_terraform(plan_path.parent))]
        for flag, name in (
            ("--inventory", "default-inventory.json"),
            ("--estimate", "default-estimate.json"),
        ):
            if flag not in extra:
                command += [flag, str(plan_path.parent / name)]
    command += list(extra)
    # check=False deliberately: a non-zero exit is the OUTCOME UNDER TEST here, not an
    # error to raise on. check=True would turn every denial into a CalledProcessError and
    # make the guard's refusals indistinguishable from the guard crashing.
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _assert_denied(result: subprocess.CompletedProcess, *, because: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"the guard EXITED 0 on a plan it must deny ({because}).\n\n"
        f"An exit code of 0 is the lane's signal to apply. Output was:\n{combined}"
    )
    assert "DENIED" in combined, (
        f"the guard exited {result.returncode} but printed no DENIED line, so a lane reading "
        f"the output rather than the exit code would not see a refusal.\nOutput:\n{combined}"
    )
    assert because in combined, (
        f"the guard denied, but not for the stated reason — {because!r} does not appear in "
        f"its output. A denial for the wrong reason passes this test while leaving the real "
        f"case uncovered, and an unexplained failure gets suppressed rather than fixed.\n"
        f"Output:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Premise: the guard exists and accepts a legitimate plan
# ---------------------------------------------------------------------------
def test_the_guard_script_exists() -> None:
    """Without this, every subprocess test below would fail for the wrong reason."""
    assert GUARD.exists(), (
        f"{GUARD} is missing. Every test in this file runs it as a subprocess; if it moved, "
        f"update this path rather than deleting the suite."
    )


def test_a_create_only_plan_is_approved(tmp_path) -> None:
    """The accept path. A guard that denies everything passes every negative test here."""
    plan = _plan(_cluster(["create"]), _vpc(["create"]), _node_group())
    result = _run(_write(tmp_path, plan))
    assert result.returncode == 0, (
        f"the guard denied a legitimate create-only plan for this workspace. A guard that "
        f"cannot approve a correct plan makes its lane unusable and gets removed rather than "
        f"fixed — the failure direction the 84e3f7ee review found in the control plane's "
        f"guard.\nOutput:\n{result.stdout}{result.stderr}"
    )
    assert "No destructive change" in result.stdout


def test_an_empty_plan_is_approved(tmp_path) -> None:
    """No changes is a legitimate outcome, and must be distinguishable from a malformed one."""
    result = _run(_write(tmp_path, _plan()))
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_guard_accepts_the_names_this_module_actually_creates() -> None:
    """The guard's naming patterns must match the Terraform's, read from source.

    This is the check that `84e3f7ee`'s review showed a derived-fixture helper alone does not
    give: if both the guard and the helper encode the same wrong prefix, every other test here
    still passes. So this reads `main.tf` directly.
    """
    main_tf = (WORKSPACES / "main.tf").read_text(encoding="utf-8")
    assert re.search(
        r'name_prefix\s*=\s*"adp-\$\{var.environment\}-spw-\$\{local.infrastructure_id\}"',
        main_tf,
    ), (
        "main.tf's local.name_prefix is no longer "
        "`adp-${var.environment}-spw-${var.workspace_name}`. workspace_ownership.name_prefix() "
        "reproduces that expression, and the guard decides ownership with it — so a rename "
        "here without a matching change there makes the guard deny every resource this "
        "module creates."
    )
    assert (
        name_prefix("dev", "tenant-alpha")
        == "adp-dev-spw-970b320a868d197402321c9d69957998"
    )


# ---------------------------------------------------------------------------
# Deletes and replacements require EXACT authorization
# ---------------------------------------------------------------------------
def test_a_delete_without_authorization_is_denied(tmp_path) -> None:
    plan = _plan(_cluster(["delete"]))
    result = _run(_write(tmp_path, plan))
    _assert_denied(result, because="no --authorize-destroy file was supplied")


@pytest.mark.parametrize(
    "actions",
    [
        ["delete"],
        ["delete", "create"],  # destroy-then-create
        ["create", "delete"],  # create-before-destroy
    ],
    ids=["delete", "replace-destroy-first", "replace-create-first"],
)
def test_every_deleting_action_set_requires_authorization(tmp_path, actions) -> None:
    """Both replacement orderings count as destruction.

    The shell guard this pattern replaces reported "No destroys in plan" for both, because it
    matched plan TEXT rather than the action list. `DESTRUCTIVE_ACTIONS` is a set intersection
    for exactly this reason, and this parametrization is what keeps it one.
    """
    plan = _plan(_cluster(actions))
    result = _run(_write(tmp_path, plan))
    _assert_denied(result, because="no --authorize-destroy file was supplied")


def test_an_exactly_matching_authorization_approves(tmp_path) -> None:
    plan = _plan(_cluster(["delete", "create"]))
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    assert result.returncode == 0, (
        f"an exactly matching authorization must approve, or the mechanism is unusable.\n"
        f"Output:\n{result.stdout}{result.stderr}"
    )
    assert "Authorization is bound to this exact plan" in result.stdout


def test_an_authorization_missing_one_destroyed_address_is_denied(tmp_path) -> None:
    """The obvious direction: something would be destroyed that nobody approved."""
    plan = _plan(_cluster(["delete"]), _vpc(["delete"]))
    flags = _authorize(
        tmp_path, plan, mutate=lambda doc: doc["destroy"].pop("aws_vpc.workspace[0]")
    )
    result = _run(_write(tmp_path, plan), *flags)
    _assert_denied(result, because="are NOT in the authorization")
    assert "aws_vpc.workspace[0]" in result.stdout


def test_a_superset_authorization_is_denied(tmp_path) -> None:
    """The direction that is easy to omit, and the reason the match is symmetric.

    An authorization listing more than the plan destroys was written against a different plan.
    Accepting it would make an approval REUSABLE — and a reusable approval approves a later,
    unreviewed plan, which is precisely what "exact plan authorization" excludes.
    """
    plan = _plan(_cluster(["delete"]))

    def add_extras(doc: dict) -> None:
        doc["destroy"]["aws_vpc.workspace[0]"] = ["delete"]
        doc["destroy"]["aws_kms_key.workspace[0]"] = ["delete"]

    result = _run(
        _write(tmp_path, plan), *_authorize(tmp_path, plan, mutate=add_extras)
    )
    _assert_denied(result, because="written against a DIFFERENT plan")


def test_an_authorization_for_a_plan_that_destroys_nothing_is_denied(tmp_path) -> None:
    """A non-empty authorization against a non-destructive plan is a plan mismatch.

    Silently ignoring it would leave an operator unable to tell whether the plan changed or
    their file was never read.
    """
    destructive = _plan(_cluster(["delete"]))
    harmless_plan = _plan(_cluster(["create"]))
    flags = _authorize(tmp_path, destructive, applied=harmless_plan)
    # Same authorization, different plan: this one only creates.
    harmless = _write(tmp_path, harmless_plan, name="applied.json")
    result = _run(harmless, *flags)
    _assert_denied(result, because="this plan does not destroy them")


def test_an_unreadable_authorization_denies(tmp_path) -> None:
    """A missing authorization file must never read as 'no restrictions'."""
    plan = _plan(_cluster(["delete"]))
    plan_path = _write(tmp_path, plan)
    result = _run(
        plan_path,
        "--authorize-destroy",
        str(tmp_path / "absent.json"),
        *_artifact_flags(tmp_path, _stub_artifact(tmp_path, plan_path)),
    )
    _assert_denied(result, because="could not read the destroy authorization")


def test_an_empty_authorization_denies_a_destructive_plan(tmp_path) -> None:
    """An empty file authorizes nothing, which is not the same as authorizing everything."""
    plan = _plan(_cluster(["delete"]))
    plan_path = _write(tmp_path, plan)
    auth = tmp_path / "auth.json"
    auth.write_text("\n", encoding="utf-8")
    result = _run(
        plan_path,
        "--authorize-destroy",
        str(auth),
        *_artifact_flags(tmp_path, _stub_artifact(tmp_path, plan_path)),
    )
    _assert_denied(result, because="is empty")


# ===========================================================================
# REVIEW FINDING W9-04: an address list is not authorization for an exact plan
# ===========================================================================
# The pre-repair mechanism compared a set of Terraform addresses, symmetrically, and its own
# docstring claimed "a reusable approval is not an approval of this plan". The comparison made
# every approval reusable across exactly the substitutions that matter, because two materially
# different plans can carry identical destroyed address sets.
#
# WHICH SUBSTITUTIONS ACTUALLY WORKED WAS MEASURED, NOT ASSUMED.
#
# The guard at 0ec565d5 (this branch's previous commit, W9-03 repaired) was restored beside the
# module's real .tf files and driven with the plain address list it accepted. It exited 0 —
# "Confirmed: every changed resource belongs to this workspace", approved — on:
#
#   *  a plan whose node group went from 2x m6i.large to 40x m6i.2xlarge, a twentyfold capacity
#      change, under the authorization reviewed for the smaller one. Identical destroyed
#      address set, so the symmetric comparison saw nothing.
#   *  a cluster REPLACEMENT under an authorization reviewed for a cluster DELETE.
#   *  the same plan applied to a different ACCOUNT. `--aws-region` was not an accepted flag at
#      all, so the region could not be bound even in principle.
#
# Two substitutions were already caught, and the controls below say so rather than claiming
# credit: `environment` and `workspace_name` are segments of every resource name, so W9-03's
# exact-name check refuses them before the authorization is read.
#
# Each control supplies an authorization valid in EVERY respect except the one under test —
# built by the guard's own `build_authorization`, then mutated in one field — so none can pass
# because of an unrelated refusal. `test_an_exactly_matching_authorization_approves` and
# `test_a_genuine_full_teardown_with_exact_authorization_is_approved` are the positive half: a
# binding set too strict to authorize a real 30-address teardown would fail there.
# ---------------------------------------------------------------------------
def test_a_changed_plan_with_an_identical_address_set_needs_fresh_authorization(
    tmp_path,
) -> None:
    """W9-04's stated test, and the one the pre-repair mechanism could not pass.

    Two plans, identical destroyed address sets — `aws_eks_cluster.workspace` in both — and a
    materially different diff: the node group's ceiling goes from 2 to 40 instances and its
    instance type from m6i.large to m6i.2xlarge, a twentyfold capacity change. Under an
    address-set approval the operator's review of the first plan silently authorized the second.
    """
    reviewed = _plan(_cluster(["delete", "create"]), _node_group(max_size=2))
    changed = _plan(
        _cluster(["delete", "create"]),
        _node_group(instance_types=["m6i.2xlarge"], max_size=40),
    )
    flags = _authorize(tmp_path, reviewed, applied=changed)
    changed_path = _write(tmp_path, changed, name="applied.json")

    # The premise: the two plans really do destroy the same addresses. Without this the test
    # could pass because the address comparison caught it, proving nothing about the digest.
    def destroyed(plan: dict) -> set[str]:
        return {
            change["address"]
            for change in plan["resource_changes"]
            if "delete" in change["change"]["actions"]
        }

    assert destroyed(reviewed) == destroyed(changed) == {"aws_eks_cluster.workspace"}, (
        "the two fixtures no longer share a destroyed address set, so this test would pass "
        "via the address comparison and stop covering the plan-digest binding."
    )

    result = _run(changed_path, *flags)
    _assert_denied(result, because="bound to a DIFFERENT plan")


def test_an_authorization_for_another_target_is_denied(tmp_path) -> None:
    """An approval does not transfer between accounts, regions, environments or tenants.

    Parametrized over all four bindings because they share a mechanism and a failure: each is
    mutated on an otherwise-valid authorization for the same plan.

    Which of the four were genuinely unguarded was MEASURED against the pre-repair guard rather
    than assumed, using a node-group deletion (identified by name, carrying no ARN):

    *   `account_id` and `aws_region` — **approved**. Neither appears in a resource name or a
        Terraform address, and `--aws-region` was not even an accepted flag, so nothing in the
        guard could have caught an approval reviewed for one and applied to another. These two
        are the W9-04 reproduction.
    *   `environment` and `workspace_name` — already denied, incidentally: both are segments of
        `local.name_prefix`, so W9-03's exact-name check catches the substitution before the
        authorization is read. They are asserted here anyway, because that defence depends on
        every destroyed resource carrying a checkable name, and the binding should not rest on
        a property of the resource set.
    """
    plan = _plan(_cluster(["delete"]))
    for name, foreign in (
        ("account_id", OTHER_ACCOUNT),
        ("aws_region", OTHER_REGION),
        ("environment", "prod"),
        ("workspace_name", OTHER_WORKSPACE),
    ):
        flags = _authorize(
            tmp_path, plan, mutate=lambda doc, n=name, v=foreign: doc.__setitem__(n, v)
        )
        result = _run(_write(tmp_path, plan), *flags)
        _assert_denied(result, because=f"This authorization is for {name}={foreign!r}")


def test_approving_a_delete_does_not_approve_a_replacement(tmp_path) -> None:
    """Same address, same "destroyed" status, different consequence.

    Deleting the cluster and replacing it are both `delete`-containing action sets at the same
    address, so an address-set approval covered both. They are not the same decision: a
    replacement issues a new OIDC issuer URL, so every IRSA role trusting the old one stops
    working — while the workspace appears to still exist.
    """
    plan = _plan(_cluster(["delete", "create"]))
    flags = _authorize(
        tmp_path,
        plan,
        mutate=lambda doc: doc["destroy"].__setitem__(
            "aws_eks_cluster.workspace", ["delete"]
        ),
    )
    result = _run(_write(tmp_path, plan), *flags)
    _assert_denied(result, because="is not authorization for the other")
    assert "issues a NEW OIDC issuer" in result.stdout, (
        f"the refusal must name what the unapproved replacement would do, or 'actions differ' "
        f"reads as a formality.\nOutput:\n{result.stdout}"
    )


def test_an_authorization_in_the_pre_w9_04_format_is_refused(tmp_path) -> None:
    """The old plain-address-list format must not be silently accepted.

    It parses as "not JSON", and the important part is that it CANNOT read as a valid
    authorization: if a stale address list were tolerated for compatibility, every approval
    written before this repair would keep working with exactly the weakness the repair removed.
    """
    plan = _plan(_cluster(["delete"]))
    plan_path = _write(tmp_path, plan)
    auth = tmp_path / "auth.txt"
    auth.write_text(
        "# reviewed against plan.json, 2026-09-20\naws_eks_cluster.workspace\n",
        encoding="utf-8",
    )
    result = _run(
        plan_path,
        "--authorize-destroy",
        str(auth),
        *_artifact_flags(tmp_path, _stub_artifact(tmp_path, plan_path)),
    )
    _assert_denied(result, because="is not valid JSON")
    assert "--emit-authorization" in result.stdout, (
        f"a refusal of the old format must say how to produce the new one.\n"
        f"Output:\n{result.stdout}"
    )


def test_an_authorization_missing_a_binding_is_refused(tmp_path) -> None:
    """A field-by-field check that each binding is REQUIRED, not merely compared when present.

    A `document.get(name) != actual` comparison would pass an authorization that simply omitted
    the field if `actual` were also empty, and an approval whose scope is unstated is not an
    approval. So each is removed in turn from an otherwise-valid document.
    """
    plan = _plan(_cluster(["delete"]))
    for binding in (
        "plan_sha256",
        # The SAVED ARTIFACT's digest is in this list, not merely alongside it. It is the binding
        # W9-04's second follow-up added, and it is the only one that distinguishes the reviewed
        # plan file from another file with an identical rendering — so an authorization that
        # omitted it would be an approval of a rendering, with the applied object unbound.
        "plan_file_sha256",
        "account_id",
        "aws_region",
        "environment",
        "workspace_name",
    ):
        flags = _authorize(tmp_path, plan, mutate=lambda doc, b=binding: doc.pop(b))
        result = _run(_write(tmp_path, plan), *flags)
        _assert_denied(result, because="is missing or has empty")


def test_an_authorization_with_an_unknown_schema_version_is_refused(tmp_path) -> None:
    """A future format must not be interpreted under this version's rules.

    An authorization written against a different set of guarantees may bind fewer things while
    looking valid; refusing is the only safe reading of a version this guard does not implement.
    """
    plan = _plan(_cluster(["delete"]))
    flags = _authorize(
        tmp_path, plan, mutate=lambda doc: doc.__setitem__("schema_version", 99)
    )
    result = _run(_write(tmp_path, plan), *flags)
    _assert_denied(result, because="requires 3")


def test_emitting_an_authorization_is_not_approving_one(tmp_path) -> None:
    """`--emit-authorization` must never be an apply path.

    A flag that both produced the approval and satisfied it would make the review step
    disappear: a lane could pass it unconditionally and every destroy would self-authorize. So
    emitting writes the document and the plan is STILL denied for having no authorization.
    """
    plan = _plan(_cluster(["delete"]))
    plan_path = _write(tmp_path, plan)
    out = tmp_path / "emitted.json"
    result = _run(
        plan_path,
        "--emit-authorization",
        str(out),
        *_artifact_flags(tmp_path, _stub_artifact(tmp_path, plan_path)),
    )
    _assert_denied(result, because="no --authorize-destroy file was supplied")
    assert out.exists(), (
        "the document must still be written, so an operator can review it"
    )
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["destroy"] == {"aws_eks_cluster.workspace": ["delete"]}
    assert document["plan_sha256"] and len(document["plan_sha256"]) == 64


def test_an_authorization_cannot_be_bound_to_an_empty_target(tmp_path) -> None:
    """A binding to "" is not a binding, so the flags it needs are required, not defaulted.

    Without this the account and region checks would compare "" to "" and pass for every lane
    that omitted them — the binding present in the code and absent in effect.
    """
    plan = _plan(_cluster(["delete"]))
    result = _run(
        _write(tmp_path, plan),
        "--emit-authorization",
        str(tmp_path / "emitted.json"),
        account=None,
        region=None,
    )
    _assert_denied(result, because="--account-id, --aws-region is required")


# ===========================================================================
# W9-04 FOLLOW-UPS: the exact saved artifact, and a target the PLAN establishes
# ===========================================================================
# The review of attempt 3 found the W9-04 repair still defeatable two ways, and both were
# reproduced against that head before being repaired:
#
#   1. A native us-east-1 plan was EMITTED AS eu-west-1. The emitted document took its target from
#      the caller's `--account-id` / `--aws-region` flags, and `_check_authorization` then compared
#      the document against those same flags. Both sides of the comparison came from one source,
#      so it could not fail: run the guard twice with `--aws-region eu-west-1` and the approval was
#      internally consistent and pointed at the wrong region. Repaired by `verify_target`, which
#      establishes each field from the PLAN's own `variables` and resource ARNs and refuses when
#      the flags disagree with it.
#   2. A destructive JSON-ONLY plan was accepted with NO SAVED BINARY IN EXISTENCE. `plan_sha256`
#      digested the `--plan-json` document, which is a rendering; `terraform apply` consumes the
#      saved plan artifact. Nothing bound the artifact, so the reviewed plan and the applied plan
#      did not have to be the same object. Repaired by requiring `--plan-file`, verifying it is a
#      real saved plan, re-deriving its JSON, and binding its digest as `plan_file_sha256`.
#
# The controls below pin each, plus the properties the repair depends on: that the second digest is
# not redundant with the first, that the actions are compared IN ORDER, and that the review
# evidence names concrete destroyed identities rather than only addresses.
# ---------------------------------------------------------------------------
def test_a_plan_whose_own_region_contradicts_the_flag_is_refused(tmp_path) -> None:
    """DEFECT 1 — the wrong-region approval, reproduced exactly as the review described it.

    The plan is a native us-east-1 plan: its `variables.aws_region` says us-east-1 and its
    cluster ARN's region field says us-east-1. The guard is then run claiming eu-west-1.

    On the reviewed head this EMITTED a document saying eu-west-1 and approved it, because the
    document's region and the region it was checked against were both `args.aws_region`. The plan
    is the authority now: it is the object that gets applied.
    """
    plan = _plan(_cluster(["delete"]), region=REGION)
    result = _run(
        _write(tmp_path, plan),
        "--emit-authorization",
        str(tmp_path / "emitted.json"),
        *_artifact_flags(tmp_path, _stub_artifact(tmp_path, _write(tmp_path, plan))),
        region=OTHER_REGION,
    )
    _assert_denied(result, because=f"but the PLAN says aws_region={REGION!r}")
    assert "wrong-region approval finding W9-04 reproduced" in (
        result.stdout + result.stderr
    ), (
        f"the refusal must name the finding it closes: the whole defect was a comparison that "
        f"could not fail, and a message that says only 'region mismatch' invites the next author "
        f"to 'fix' it by trusting the flag again.\nOutput:\n{result.stdout}{result.stderr}"
    )


def test_a_destructive_plan_with_no_saved_artifact_is_refused(tmp_path) -> None:
    """DEFECT 2 — a destructive JSON-only plan, authorized with no saved binary in existence.

    No `--plan-file` at all. On the reviewed head this was the normal invocation and it approved:
    the digest covered the JSON document handed to the guard, which is not the file
    `terraform apply` reads.
    """
    plan = _plan(_cluster(["delete"]))
    plan_path = _write(tmp_path, plan)
    artifact = _stub_artifact(tmp_path, plan_path)
    document = _authorization_for(plan_path, artifact)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(document, indent=2), encoding="utf-8")

    # Deliberately NOT passing _artifact_flags: the absence is the subject.
    result = _run(
        plan_path,
        "--authorize-destroy",
        str(auth),
        "--inventory",
        str(tmp_path / "i.json"),
        "--estimate",
        str(tmp_path / "e.json"),
        complete=False,
    )
    _assert_denied(result, because="--plan-file is required")
    assert "only its rendering" in (result.stdout + result.stderr), (
        f"the refusal must explain that the JSON is a RENDERING of the artifact, or the reader "
        f"concludes the flag is bureaucratic and adds a way around it.\n"
        f"Output:\n{result.stdout}{result.stderr}"
    )


def test_a_json_document_passed_as_the_artifact_is_refused(tmp_path) -> None:
    """The artifact must be a real saved plan, not the JSON renamed.

    The obvious way to satisfy a `--plan-file` requirement without understanding it is to pass the
    plan JSON again. A saved plan is a zip containing a `tfplan` member; a JSON document is not,
    and accepting one would make the second digest a digest of the same object as the first.
    """
    plan = _plan(_cluster(["delete"]))
    plan_path = _write(tmp_path, plan)
    result = _run(
        plan_path,
        "--emit-authorization",
        str(tmp_path / "emitted.json"),
        "--plan-file",
        str(plan_path),
        "--terraform-binary",
        str(_stub_terraform(tmp_path)),
    )
    _assert_denied(result, because="is not a Terraform saved plan")


def test_a_json_that_does_not_derive_from_the_artifact_is_refused(tmp_path) -> None:
    """The reviewed JSON and the bound artifact must be the same plan.

    Otherwise an operator reviews the rendering of one plan while the approval authorizes applying
    another — the two-object gap the artifact binding exists to close, arriving by a different
    route than a digest mismatch.
    """
    reviewed = _plan(_cluster(["delete"]))
    other = _plan(_cluster(["delete"]), _node_group(max_size=40))
    reviewed_path = _write(tmp_path, reviewed)
    foreign_artifact = _stub_artifact(
        tmp_path, _write(tmp_path, other, name="other.json"), name="other.tfplan"
    )
    result = _run(
        reviewed_path,
        "--emit-authorization",
        str(tmp_path / "emitted.json"),
        *_artifact_flags(tmp_path, foreign_artifact),
    )
    _assert_denied(result, because="is NOT a rendering of")
    assert "they disagree on ['resource_changes', 'variables']" in (
        result.stdout + result.stderr
    ), (
        f"the refusal must name WHICH field differs. 'these are different plans' leaves an "
        f"operator with two large JSON documents and no way to tell whether they hit a real "
        f"substitution or a formatting difference the guard should have tolerated.\n"
        f"Output:\n{result.stdout}{result.stderr}"
    )


def test_an_authorization_bound_to_another_artifact_is_refused(tmp_path) -> None:
    """The second digest is NOT redundant with the first — measured, not assumed.

    Distinct artifact bytes can render as identical JSON. The archive's non-rendered
    members still belong to the exact artifact approved for apply.

    So this substitutes an artifact whose rendering the guard accepts, and only
    `plan_file_sha256` can catch it. Without this control, a repair that dropped the second digest
    as "already covered by the JSON digest" would pass the whole suite.
    """
    plan = _plan(_cluster(["delete"]))
    plan_path = _write(tmp_path, plan)
    reviewed = _stub_artifact(tmp_path, plan_path, name="reviewed.tfplan")
    # Same plan content, different file. Its rendering is identical, so every derivation check
    # passes and the digest is the only difference.
    applied = _stub_artifact(tmp_path, plan_path, name="applied.tfplan")
    import zipfile

    with zipfile.ZipFile(applied, "a") as archive:
        archive.writestr(
            "tfstate-prev", "padding that changes the bytes, not the rendering"
        )

    assert _plan_digest(reviewed) != _plan_digest(applied), (
        "the two artifacts digest identically, so this test would pass without the artifact "
        "binding doing anything."
    )

    document = _authorization_for(plan_path, reviewed)
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps(document, indent=2), encoding="utf-8")
    result = _run(
        plan_path,
        "--authorize-destroy",
        str(auth),
        *_artifact_flags(tmp_path, applied),
    )
    _assert_denied(result, because="bound to a DIFFERENT saved plan ARTIFACT")


def test_create_before_destroy_is_not_authorized_by_destroy_before_create(
    tmp_path,
) -> None:
    """Ordered action comparison: `sorted()` makes these two indistinguishable.

    `['delete','create']` destroys the cluster and then builds its replacement;
    `['create','delete']` (create_before_destroy) keeps the old one alive until the new one exists.
    They sort identically, so a set or sorted comparison would let an approval for one authorize
    the other — with opposite availability consequences.
    """
    plan = _plan(_cluster(["create", "delete"]))
    flags = _authorize(
        tmp_path,
        plan,
        mutate=lambda doc: doc["destroy"].__setitem__(
            "aws_eks_cluster.workspace", ["delete", "create"]
        ),
    )
    result = _run(_write(tmp_path, plan), *flags)
    _assert_denied(result, because="same actions in a DIFFERENT ORDER")


def test_a_reformatted_plan_json_still_derives_from_its_artifact(tmp_path) -> None:
    """The derivation check compares PARSED documents, not bytes — and must.

    A byte comparison would refuse a JSON document that had been through `jq`, pretty-printed by a
    lane, or re-serialised with different key order — all of which are the same plan. A guard that
    refuses correct input gets bypassed rather than satisfied, so this pins the tolerance
    deliberately: the digest still covers the exact bytes, which is what binds the review.

    Re-rendering the same saved artifact preserves its timestamp; creating a new plan may
    change it. Only formatting and object-key order are ignored here.
    """
    plan = _plan(_cluster(["delete"]))
    artifact = _stub_artifact(tmp_path, _write(tmp_path, plan))

    reformatted = tmp_path / "reformatted.json"
    reformatted.write_text(
        json.dumps(plan, indent=4, sort_keys=True) + "\n", encoding="utf-8"
    )
    # The premise: it really is different bytes, or this proves nothing about the tolerance.
    assert reformatted.read_bytes() != (tmp_path / "plan.json").read_bytes()

    out = tmp_path / "emitted.json"
    result = _run(
        reformatted,
        "--emit-authorization",
        str(out),
        *_artifact_flags(tmp_path, artifact),
    )
    assert out.exists(), (
        f"a reformatted but semantically identical rendering was refused. The derivation check "
        f"must compare parsed documents on the fields that define the plan, or every lane that "
        f"pipes plan JSON through a formatter is blocked and the check gets removed.\n"
        f"Output:\n{result.stdout}{result.stderr}"
    )
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["plan_sha256"] == _plan_digest(reformatted), (
        "the emitted digest must cover the bytes actually reviewed, not a normalisation of them."
    )


# `terraform` is absent from the lane that runs this suite (superplane-domain-ci.yml has no
# setup-terraform step), so the real-artifact control below SKIPS there rather than failing. That
# is a deliberate trade and the reason every control above uses a double: a suite that requires a
# Terraform binary would not run in the lane it is written for.
TERRAFORM = shutil.which("terraform")

SAVED_PLAN_FIXTURE = """
variable "account_id" { type = string }
variable "aws_region" { type = string }
variable "environment" { type = string }
variable "workspace_name" { type = string }
variable "org_id" { type = string }
variable "workspace_id" { type = string }

# `terraform_data` is a BUILT-IN provider, which is what makes this fixture usable here: it needs
# no `terraform init`, no provider download, no credential and no network. Verified offline.
resource "terraform_data" "target_account_guard" {
  input = var.account_id
}
"""


@pytest.mark.skipif(
    TERRAFORM is None,
    reason=(
        "no terraform binary on PATH. This control needs a REAL saved plan, which only the "
        "Terraform binary can produce; the doubles used by the controls above cover the same "
        "code paths in the lane that has no Terraform."
    ),
)
def test_a_real_terraform_saved_plan_is_accepted_end_to_end(tmp_path) -> None:
    """The double's gap: a GENUINE `terraform plan -out` artifact must satisfy every check.

    Every other artifact control here uses a stub zip and a stub `terraform show -json`, which can
    only confirm the guard accepts the shape this file believes in. If a real saved plan's internals
    differed — a different member name, a rendering the derivation check rejected — the whole suite
    would still pass while the guard refused every real plan an operator produced.

    So this builds a module, applies it, takes a real `-destroy` plan, renders it with the real
    binary, and drives the full emit-then-approve cycle. Nothing here reaches AWS: `terraform_data`
    is the built-in provider, so apply and plan are entirely local.
    """
    module = tmp_path / "module"
    module.mkdir()
    (module / "main.tf").write_text(SAVED_PLAN_FIXTURE, encoding="utf-8")

    target = [
        "-var",
        f"account_id={ACCOUNT}",
        "-var",
        f"aws_region={REGION}",
        "-var",
        f"environment={ENVIRONMENT}",
        "-var",
        f"workspace_name={WORKSPACE}",
        "-var",
        f"org_id={ORG_ID}",
        "-var",
        f"workspace_id={WORKSPACE}",
    ]

    def terraform(*command: str) -> subprocess.CompletedProcess:
        done = subprocess.run(
            [TERRAFORM, *command],
            cwd=module,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, (
            f"`terraform {' '.join(command)}` failed while BUILDING the fixture, which is a "
            f"problem with this test rather than with the guard.\n{done.stdout}{done.stderr}"
        )
        return done

    terraform("apply", "-auto-approve", *target)
    terraform("plan", "-destroy", "-out=ws.tfplan", *target)
    rendered = terraform("show", "-json", "ws.tfplan").stdout
    plan_json = module / "ws.json"
    plan_json.write_text(rendered, encoding="utf-8")

    artifact = module / "ws.tfplan"
    emitted = tmp_path / "emitted.json"

    # Phase 1: emit. Must write the document AND still deny, because emitting is not approving.
    emit = _run(
        plan_json,
        "--emit-authorization",
        str(emitted),
        "--plan-file",
        str(artifact),
        "--terraform-binary",
        TERRAFORM,
        "--module-dir",
        str(module),
        "--expect-destroy",
    )
    assert emitted.exists(), (
        f"no authorization was emitted for a REAL saved plan. Every artifact control in this file "
        f"uses a double, so this is the only one that would notice the guard rejecting genuine "
        f"Terraform output.\nOutput:\n{emit.stdout}{emit.stderr}"
    )
    _assert_denied(emit, because="no --authorize-destroy file was supplied")

    document = json.loads(emitted.read_text(encoding="utf-8"))
    assert document["plan_file_sha256"] == _plan_digest(artifact), (
        "the emitted document must bind the digest of the artifact on disk."
    )
    assert document["destroy"] == {"terraform_data.target_account_guard": ["delete"]}
    # The target came from the plan's own variables, which is the W9-04 repair's whole point.
    assert document["aws_region"] == REGION
    assert (
        "plan variables.aws_region" in document["target_established_by"]["aws_region"]
    )

    # Phase 2: approve with the emitted document. This is the path an operator actually runs.
    approve = _run(
        plan_json,
        "--authorize-destroy",
        str(emitted),
        "--plan-file",
        str(artifact),
        "--terraform-binary",
        TERRAFORM,
        "--module-dir",
        str(module),
        "--expect-destroy",
    )
    assert approve.returncode == 0, (
        f"the guard refused a REAL saved plan under its OWN emitted authorization. A guard that "
        f"cannot approve the artifact it just authorized is unusable, and gets removed rather "
        f"than fixed.\nOutput:\n{approve.stdout}{approve.stderr}"
    )
    assert "scripts/apply_workspace_plan.py" in approve.stdout, (
        "the approval must tell the operator to apply THIS artifact rather than re-plan; "
        "re-planning between check and apply is the race the binding exists to close."
    )


def test_the_review_evidence_names_the_concrete_destroyed_identity(tmp_path) -> None:
    """ "aws_eks_cluster.workspace would be replaced" does not say WHICH cluster.

    The review asked for concrete destructive resource identities in the evidence. An address is a
    label this module's source chooses; the id and ARN are what identify the object that stops
    existing. Taken from the BEFORE side specifically — on a replacement the `after` describes the
    resource that will exist afterwards, which is not the one being destroyed.
    """
    plan = _plan(_cluster(["delete", "create"], id="cluster-id-being-destroyed"))
    result = _run(_write(tmp_path, plan))
    combined = result.stdout + result.stderr
    for expected in (
        "cluster-id-being-destroyed",
        f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{PREFIX}",
    ):
        assert expected in combined, (
            f"the destructive-change evidence does not name {expected!r}. A reviewer approving "
            f"a replacement by ADDRESS alone has not been shown which object is destroyed.\n"
            f"Output:\n{combined}"
        )


def test_a_cluster_replacement_names_its_consequence(tmp_path) -> None:
    """ "1 replacement" is not what a reviewer needs to know about an EKS cluster."""
    plan = _plan(_cluster(["delete", "create"]))
    result = _run(_write(tmp_path, plan))
    assert "issues a NEW OIDC issuer" in result.stdout, (
        f"a cluster replacement must say what it destroys. A reviewer approving "
        f"'1 replacement' has not been told that every IRSA role stops working.\n"
        f"Output:\n{result.stdout}"
    )


def test_a_destroy_plan_that_deletes_nothing_is_denied(tmp_path) -> None:
    """`--expect-destroy` on a plan with no deletions means the wrong plan file."""
    plan = _plan(_cluster(["create"]))
    result = _run(_write(tmp_path, plan), "--expect-destroy")
    _assert_denied(result, because="this plan contains no deletions")


# ---------------------------------------------------------------------------
# Ownership: cross-workspace, cross-account, and beyond-the-boundary types
# ---------------------------------------------------------------------------
def test_another_workspaces_cluster_is_denied(tmp_path) -> None:
    """Two workspaces in one account differ only in the name segment.

    This is the failure a label-based approval cannot catch: a plan run with the wrong
    `-var-file` is shaped exactly like a correct one, and every action in it is a create.
    """
    plan = _plan(_cluster(["create"], workspace=OTHER_WORKSPACE))
    result = _run(_write(tmp_path, plan))
    _assert_denied(result, because="is not the name this declaration produces")


def test_another_workspaces_tagged_vpc_is_denied(tmp_path) -> None:
    """The tag-identified path: a VPC has no name, so the Workspace tag decides."""
    plan = _plan(_vpc(["delete"], workspace=OTHER_WORKSPACE))
    result = _run(_write(tmp_path, plan))
    _assert_denied(result, because="is another one's resource")


def test_an_untagged_vpc_is_denied(tmp_path) -> None:
    """Unattributable is denied, not assumed to be ours."""
    change = _vpc(["create"])
    change["change"]["after"] = {"cidr_block": "10.64.0.0/16", "tags_all": {}}
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="carries no `WorkspaceId` tag")


def test_a_cross_environment_cluster_is_denied(tmp_path) -> None:
    """The environment is inside the prefix, so a prod resource in a dev plan is refused."""
    plan = _plan(_cluster(["delete"]))
    result = _run(_write(tmp_path, plan), environment="staging")
    _assert_denied(result, because="is not the name this declaration produces")


def test_a_resource_in_another_account_is_denied(tmp_path) -> None:
    """A workspace's resources live in the workspace's own account."""
    change = _cluster(["create"])
    change["change"]["after"]["arn"] = (
        f"arn:aws:eks:us-east-1:{OTHER_ACCOUNT}:cluster/{PREFIX}"
    )
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because=f"has an ARN in account {OTHER_ACCOUNT}")


def test_a_name_merely_containing_the_prefix_is_denied(tmp_path) -> None:
    """Anchored, not substring — failure direction 5 of the `84e3f7ee` review.

    `gateway-adp-superplane-api` passed that guard by merely CONTAINING the domain prefix.
    """
    change = _cluster(["delete"])
    change["change"]["before"]["name"] = f"gateway-{PREFIX}-api"
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="is not the name this declaration produces")


def test_a_platform_owned_resource_is_denied(tmp_path) -> None:
    """A type whose destruction reaches outside the workspace, in a workspace plan."""
    change = {
        "address": "aws_s3_bucket.state",
        "change": {
            "actions": ["delete"],
            "before": {"bucket": f"{PREFIX}-state"},
            "after": None,
        },
    }
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="the Terraform state backend is an S3 bucket")


def test_an_organizations_account_is_denied(tmp_path) -> None:
    """#5530's rule: closing an account is never a consequence of removing a workspace."""
    change = {
        "address": "aws_organizations_account.tenant",
        "change": {"actions": ["delete"], "before": {"id": ACCOUNT}, "after": None},
    }
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="account closure is never a consequence")


def test_a_nested_module_address_does_not_hide_the_type(tmp_path) -> None:
    """Failure direction 2: a guard anchored at the start of the address never sees the type.

    `module.core.aws_s3_bucket.state` begins with `module.`, so a prefix check reads no type
    at all and the check it thought it was doing did not happen.
    """
    change = {
        "address": "module.core.module.net.aws_s3_bucket.state",
        "change": {
            "actions": ["delete"],
            "before": {"bucket": "adp-state"},
            "after": None,
        },
    }
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="the Terraform state backend is an S3 bucket")


def test_a_near_miss_resource_type_is_denied(tmp_path) -> None:
    """Failure direction 3: `aws_iam_role_policies_exclusive` DELETES inline role policies.

    It matches an unanchored alternation written as `aws_iam_role_policy|...`, and its whole
    purpose is removing policies absent from configuration.
    """
    change = {
        "address": "aws_iam_role_policies_exclusive.cluster",
        "change": {
            "actions": ["create"],
            "before": None,
            "after": {"role_name": f"{PREFIX}-cluster-role"},
        },
    }
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="not in this workspace module's allowlist")


def test_an_address_the_module_does_not_declare_is_denied(tmp_path) -> None:
    """An allowed type at an undeclared address is a stale or foreign state entry.

    `terraform destroy` acts on STATE, not on source — so an entry left behind by a removed
    declaration is invisible to every source-reading check and still gets destroyed.
    """
    change = {
        "address": "aws_iam_role.legacy_admin",
        "change": {
            "actions": ["delete"],
            "before": {"name": f"{PREFIX}-legacy"},
            "after": None,
        },
    }
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because='no `resource "aws_iam_role" "legacy_admin"`')


def test_an_unapproved_data_source_is_denied(tmp_path) -> None:
    """Reading is not owning, but an unreviewed read is still a coupling."""
    change = {
        "address": "data.terraform_remote_state.platform",
        "change": {"actions": ["read"], "before": None, "after": {}},
    }
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="outside this module's approved read set")


def test_drift_on_a_foreign_resource_is_denied(tmp_path) -> None:
    """Drift is validated too: it reveals what the STATE contains, not just what changes."""
    plan = _plan(
        _cluster(["create"]),
        drift=[
            {
                "address": "aws_vpc.workspace[0]",
                "change": {
                    "actions": ["update"],
                    "before": {"tags_all": _owned_tags(workspace=OTHER_WORKSPACE)},
                    "after": {"tags_all": _owned_tags(workspace=OTHER_WORKSPACE)},
                },
            }
        ],
    )
    result = _run(_write(tmp_path, plan))
    _assert_denied(result, because="is another one's resource")


# ===========================================================================
# REVIEW FINDING W9-03: the three reproductions, as permanent negative controls
# ===========================================================================
# Each test marked `THE W9-03 CONTROL` below was RUN against the pre-repair guard and confirmed
# to be APPROVED there — the guard printed "Confirmed: every changed resource belongs to this
# workspace" and exited 0 on a plan it must deny.
#
# Every one of them is a DESTROY with a matching `--authorize-destroy` file (see `_authorize`).
# That is load-bearing, and it was measured rather than assumed: without the authorization the
# pre-repair guard refuses these plans anyway — for the unrelated reason that an unauthorized
# destroy is always refused — and a control that fails for the wrong reason would have been
# recorded as reproducing a defect it never touched. Supplying the authorization leaves
# ownership as the only remaining gate.
#
# They are kept because the three defects had three different causes, so fixing one says
# nothing about the others:
#
#   1. Ownership was decided by the prefix pattern
#      `^adp-<env>-spw-<workspace>(-[a-z0-9][a-z0-9-]*)?$`. Workspace names are themselves
#      `[a-z0-9-]`, so for workspace `alpha` that pattern accepts `adp-dev-spw-alpha-prod` —
#      workspace `alpha-prod`'s cluster. Repaired by deriving the EXACT name each declaration
#      produces (`expected_names`) and comparing for equality.
#   2. Only the `Workspace` tag was compared, never the environment. A VPC tagged
#      `Environment=prod` was accepted by a plan for `environment=dev`. Repaired by checking
#      both identity tags, on both sides of the change.
#   3. `aws_iam_role_policy_attachment`, `aws_route_table_association` and
#      `aws_vpc_security_group_egress_rule` were decided PURELY on the Terraform address —
#      which is a label this module's source chooses, not a fact about AWS. Repaired by also
#      resolving what each one POINTS AT against the resources this same plan verified.
#
# The positive halves are in the same section on purpose. A guard repaired by refusing
# everything would pass all six negative tests here, and the genuine-shaped plans below are
# what make that reading fail: a full first apply, a full teardown, and a supplied-networking
# apply must each still be approved.
# ---------------------------------------------------------------------------


def _change(address: str, actions: list[str], values: dict) -> dict:
    """One `resource_changes` entry with `before`/`after` populated the way Terraform does."""
    before = values if {"delete", "update", "no-op"} & set(actions) else None
    after = values if actions != ["delete"] else None
    return {
        "address": address,
        "change": {"actions": actions, "before": before, "after": after},
    }


def _authorization_for(
    plan_path: Path,
    artifact_path: Path,
    *,
    workspace: str = WORKSPACE,
    environment: str = ENVIRONMENT,
    account: str = ACCOUNT,
    region: str = REGION,
) -> dict:
    """The authorization document for this exact plan, built by the guard's OWN code.

    Assembled by calling `build_authorization` and `_plan_digest` from the guard rather than
    hand-writing the JSON in this file. Hand-writing it would mean these fixtures agreed with
    the test author about the format while the real emitter drifted, so the positive tests
    would keep passing with an emitter no operator could use.

    It is built in-process rather than by running `--emit-authorization` because most controls
    below supply an authorization for a plan the guard MUST deny — an ownership violation, a
    foreign target — and the emitter refuses those plans, correctly: it only writes a document
    for a plan that is otherwise safe. Running it would leave those controls unable to reach
    the gate they exist to test.

    `artifact_path` is the stand-in saved plan these fixtures pass as `--plan-file`. Its DIGEST is
    what the document binds, and `_plan_digest` is content-addressed, so a stub file digests
    correctly without being a real plan — which is what lets the hand-built-fixture controls below
    reach the authorization gate at all. The controls that require a genuine Terraform artifact
    are separate and marked as such; see `_saved_plan_fixture`.

    `test_the_emitted_document_is_exactly_what_these_fixtures_build` closes the gap that
    in-process assembly opens, by comparing this against the real `--emit-authorization` output
    for a plan the guard does approve.
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    report = validate_plan(
        plan,
        environment=environment,
        workspace_name=workspace,
        org_id=ORG_ID,
        workspace_id=workspace,
        account_id=account,
    )
    # The target and its evidence come from the guard's own `verify_target`, not from a literal
    # written here. The evidence strings record HOW each field was established — "plan
    # variables.account_id; corroborated by 1 resource ARN value(s)" — and that text varies with
    # the fixture's ARNs. Hand-writing it made
    # `test_the_emitted_document_is_exactly_what_these_fixtures_build` fail for the right reason:
    # these fixtures disagreed with the real emitter about the document's content.
    _verified, evidence = verify_target(
        plan,
        {
            "account_id": account,
            "aws_region": region,
            "environment": environment,
            "workspace_name": workspace,
            "org_id": ORG_ID,
            "workspace_id": workspace,
        },
    )
    return build_authorization(
        plan_sha256=_plan_digest(plan_path),
        plan_file_sha256=_plan_digest(artifact_path),
        plan_file_name=artifact_path.name,
        account_id=account,
        aws_region=region,
        environment=environment,
        workspace_name=workspace,
        org_id=ORG_ID,
        workspace_id=workspace,
        target_evidence=evidence,
        destructive_actions=report.destructive_actions,
        destructive_identities={
            change["address"]: destructive_identity(change)
            for change in plan.get("resource_changes") or []
            if change["address"] in report.destructive_actions
        },
    )


def test_the_emitted_document_is_exactly_what_these_fixtures_build(tmp_path) -> None:
    """`--emit-authorization` and `_authorization_for` must not be able to drift apart.

    Every control below is parametrized by the in-process document, so if the emitter wrote
    something different the whole authorization suite would be testing a format that does not
    exist. Compared for a genuine full teardown — the realistic case an operator emits for —
    and on the parsed object, so key ORDER (which affects the file's bytes but not its meaning)
    is not asserted.

    The exit code is deliberately NOT asserted to be 0: emitting for a destructive plan writes
    the document and still denies the apply, which is
    `test_emitting_an_authorization_is_not_approving_one`'s subject. What matters here is the
    document's CONTENT.
    """
    plan = _genuine_plan(creating=False)
    plan_path = _write(tmp_path, plan)
    artifact = _stub_artifact(tmp_path, plan_path)
    out = tmp_path / "emitted.json"
    result = _run(
        plan_path,
        "--emit-authorization",
        str(out),
        *_artifact_flags(tmp_path, artifact),
    )
    assert out.exists(), (
        f"--emit-authorization wrote no document for a genuine full teardown.\n"
        f"Output:\n{result.stdout}{result.stderr}"
    )
    assert json.loads(out.read_text(encoding="utf-8")) == _authorization_for(
        plan_path, artifact
    ), (
        "the document --emit-authorization writes differs from the one these fixtures build "
        "with the guard's own build_authorization(). Whichever is wrong, every authorization "
        "control below is now testing a format that is not the one operators receive."
    )


def _authorize(
    tmp_path: Path,
    plan: dict,
    *,
    workspace: str = WORKSPACE,
    environment: str = ENVIRONMENT,
    account: str = ACCOUNT,
    region: str = REGION,
    mutate=None,
    applied: dict | None = None,
) -> list[str]:
    """Emit an authorization bound to this exact plan and return the flags that pass it back.

    Every W9-03 control is a DESTROY, because destruction is where misattribution costs
    something irreversible. But a destroy with no authorization is refused for that reason
    alone, and a control that passes on the pre-repair source for the wrong reason proves
    nothing — measured, not assumed: the pre-repair guard exits 0 on the prefix-collision plan
    once this file is supplied, printing "Confirmed: every changed resource belongs to this
    workspace". So each control supplies a matching authorization, leaving OWNERSHIP as the only
    remaining gate.

    `mutate` receives the emitted document and may alter it in place — that is how the W9-04
    controls below build an authorization that is valid in every respect except the one under
    test.

    The returned flags include the `--plan-file` artifact and the stub `--terraform-binary` that
    re-derives it, because the guard now REQUIRES a saved artifact for anything that authorizes
    (W9-04). Without them every control here would be refused for the missing artifact rather than
    for the thing it tests — a denial for the wrong reason, which `_assert_denied` is written to
    catch but which would leave each real case uncovered.
    """
    plan_path = _write(tmp_path, plan)
    artifact = _stub_artifact(tmp_path, plan_path)
    document = _authorization_for(
        plan_path,
        artifact,
        workspace=workspace,
        environment=environment,
        account=account,
        region=region,
    )
    if mutate is not None:
        mutate(document)
    path = tmp_path / "auth.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    # `applied` is the plan the guard will actually be RUN against, when that differs from the
    # reviewed one. Its artifact has to be the one passed to --plan-file: the artifact must always
    # match the --plan-json document (the guard re-derives one from the other and refuses a
    # mismatch), so the substitution under test is "authorization for plan A, invocation on plan
    # B", not "JSON for A with B's artifact" — which is a different, separately-tested defect.
    if applied is not None:
        artifact = _stub_artifact(
            tmp_path,
            _write(tmp_path, applied, name="applied.json"),
            name="applied.tfplan",
        )
    return [
        "--authorize-destroy",
        str(path),
        *_artifact_flags(tmp_path, artifact),
    ]


def _artifact_flags(tmp_path: Path, artifact: Path) -> list[str]:
    """The `--plan-file` / `--terraform-binary` pair every authorizing invocation needs."""
    return [
        "--plan-file",
        str(artifact),
        "--terraform-binary",
        str(_stub_terraform(tmp_path)),
    ]


def _owned_tags(*, workspace: str = WORKSPACE, environment: str = ENVIRONMENT) -> dict:
    """`tags_all` as the provider's default_tags produce it from main.tf's local.common_tags."""
    return {
        "Project": "adp",
        "Environment": environment,
        "Module": "domain-apps/superplane",
        "ManagedBy": "terraform",
        "Component": "superplane-workspace",
        "DomainApp": "superplane",
        "Workspace": workspace,
        "WorkspaceId": workspace,
        "OrgId": ORG_ID,
        "NetworkOwnership": "adp-created",
        "CostCenter": "platform",
    }


def _genuine_network_context(plan, creating, networking):
    """Render the known VPC scope and computed references a native plan carries."""
    plan["variables"]["networking_mode"] = {"value": networking}
    plan["variables"]["supplied_vpc_id"] = {
        "value": "vpc-0suppliedbyowner" if networking == "supplied" else ""
    }
    plan["variables"]["supplied_private_subnet_ids"] = {
        "value": ["subnet-supplied-a", "subnet-supplied-b"]
    }
    configurations = []
    for change in plan["resource_changes"]:
        address = change["address"]
        detail = change["change"]
        expressions = {}
        if address in {
            "aws_security_group.cluster",
            "aws_security_group.private_sts[0]",
        }:
            expressions["vpc_id"] = {"references": ["local.vpc_id"]}
            for side in ("before", "after"):
                if isinstance(detail.get(side), dict):
                    if networking == "supplied":
                        detail[side]["vpc_id"] = "vpc-0suppliedbyowner"
                    elif not creating:
                        detail[side]["vpc_id"] = "vpc-0workspace"
            if networking == "owned" and creating:
                detail["after_unknown"] = {"vpc_id": True}
        elif address == "aws_vpc_endpoint.private_sts[0]":
            expressions = {
                "vpc_id": {"references": ["local.vpc_id"]},
                "subnet_ids": {"references": ["local.private_subnet_ids"]},
                "security_group_ids": {
                    "references": [
                        "aws_security_group.private_sts[0].id",
                        "aws_security_group.private_sts",
                    ]
                },
            }
        elif address == "aws_vpc_security_group_ingress_rule.private_sts_nodes[0]":
            expressions = {
                "security_group_id": {
                    "references": [
                        "aws_security_group.private_sts[0].id",
                        "aws_security_group.private_sts",
                    ]
                },
                "referenced_security_group_id": {
                    "references": [
                        "aws_eks_cluster.workspace.vpc_config[0].cluster_security_group_id",
                        "aws_eks_cluster.workspace",
                    ]
                },
            }
        elif address == "aws_vpc_security_group_egress_rule.cluster_all":
            expressions["security_group_id"] = {
                "references": [
                    "aws_security_group.cluster.id",
                    "aws_security_group.cluster",
                ]
            }
        elif address.startswith("aws_route_table_association."):
            tier = address.split(".")[1].split("[")[0]
            expressions = {
                "subnet_id": {"references": [f"aws_subnet.{tier}", "count.index"]},
                "route_table_id": {
                    "references": [
                        f"aws_route_table.{tier}[0].id",
                        f"aws_route_table.{tier}[0]",
                        f"aws_route_table.{tier}",
                    ]
                },
            }
        if address in ("aws_eks_cluster.workspace", "aws_eks_node_group.default"):
            is_cluster = address == "aws_eks_cluster.workspace"
            role = "cluster" if is_cluster else "node"
            role_field = "role_arn" if is_cluster else "node_role_arn"
            expressions[role_field] = {
                "references": [f"aws_iam_role.{role}.arn", f"aws_iam_role.{role}"]
            }
            subnet_expr = {"references": ["local.private_subnet_ids"]}
            if is_cluster:
                expressions["vpc_config"] = [
                    {
                        "subnet_ids": subnet_expr,
                        "security_group_ids": {
                            "references": [
                                "aws_security_group.cluster.id",
                                "aws_security_group.cluster",
                            ]
                        },
                    }
                ]
            else:
                expressions["subnet_ids"] = subnet_expr
                expressions["launch_template"] = [
                    {
                        "id": {
                            "references": [
                                "aws_launch_template.node.id",
                                "aws_launch_template.node",
                            ]
                        }
                    }
                ]
            for side in ("before", "after"):
                if not isinstance(detail.get(side), dict):
                    continue
                vals = detail[side]
                subnets = (
                    ["subnet-supplied-a", "subnet-supplied-b"]
                    if networking == "supplied"
                    else ["subnet-0private0", "subnet-0private1"]
                )
                if not creating:
                    vals[role_field] = (
                        f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-{role}-role"
                    )
                if is_cluster:
                    vals["vpc_config"] = [{}]
                    if not creating:
                        vals["vpc_config"][0]["security_group_ids"] = ["sg-0cluster"]
                        vals["vpc_config"][0]["cluster_security_group_id"] = (
                            "sg-0123456789abcdef0"
                        )
                    if not creating or networking == "supplied":
                        vals["vpc_config"][0]["subnet_ids"] = subnets
                else:
                    vals["launch_template"] = [{"id": "lt-test-root", "version": "1"}]
                    if not creating or networking == "supplied":
                        vals["subnet_ids"] = subnets
            if creating:
                mask = detail.setdefault("after_unknown", {})
                mask[role_field] = True
                if is_cluster:
                    mask["vpc_config"] = [{"security_group_ids": True}]
                    if networking == "owned":
                        mask["vpc_config"][0]["subnet_ids"] = True
                elif networking == "owned":
                    mask["subnet_ids"] = True
        if address == "aws_eks_addon.vpc_cni":
            expressions["service_account_role_arn"] = {
                "references": ["aws_iam_role.vpc_cni.arn", "aws_iam_role.vpc_cni"]
            }
        if expressions:
            configurations.append(
                {"address": address.split("[")[0], "expressions": expressions}
            )
            if creating:
                for field in expressions:
                    if not detail["after"].get(field):
                        detail.setdefault("after_unknown", {})[field] = True
    plan["configuration"] = {"root_module": {"resources": configurations}}
    return plan


def _genuine_plan(*, creating: bool, networking: str = "owned") -> dict:
    """Every change a real `terraform plan` for this module emits, at every declared address.

    Two axes, because the stricter W9-03 rules could each be "satisfied" by denying a shape a
    real deploy produces:

    *   `creating=True` is a first apply. Every AWS-assigned id is UNKNOWN, so every
        relationship field (`subnet_id`, `route_table_id`, `security_group_id`) is absent from
        the plan. That must be approved — a guard that required a resolvable target on a create
        would deny every legitimate first apply, which is why `_check_relationship_targets`
        tolerates unknown on a non-destructive change and refuses it only on a destructive one.
    *   `creating=False` is a full teardown. Every id IS known, so this is the case that
        exercises the owned-id resolution: each association must resolve to a subnet and route
        table THIS plan verified, and the egress rule to this workspace's own security group.

    `networking="supplied"` drops every network resource (network.tf gates all of them on
    `local.owns_network`) and reads the supplied VPC instead. Both modes are covered because the
    mode changes which addresses legitimately appear, and a rule tuned to one mode's shape would
    reject the other's.

    The addresses are checked against `declared_addresses()` by
    `test_the_genuine_plan_covers_every_declaration_the_module_makes`, so this fixture cannot
    quietly stop representing a real plan when a resource is added to the module.
    """
    actions = ["create"] if creating else ["delete"]

    def ident(value: str) -> dict:
        # Absent on a create: the plan records an AWS-assigned id as unknown, and Terraform
        # omits unknown attributes from `after` rather than emitting a placeholder.
        return {} if creating else {"id": value}

    def arn(value: str) -> dict:
        return {} if creating else {"arn": value}

    tags = {"tags_all": _owned_tags()}
    changes: list[dict] = [
        # Creates nothing in AWS; carries main.tf's target-account precondition.
        _change("terraform_data.target_account_guard", actions, {}),
        _change("terraform_data.topology_guard", actions, {}),
        _change(
            "aws_kms_key.workspace[0]",
            actions,
            {**tags, **arn(f"arn:aws:kms:us-east-1:{ACCOUNT}:key/1234abcd")},
        ),
        _change(
            "aws_kms_alias.workspace[0]", actions, {"name": f"alias/{PREFIX}-secrets"}
        ),
        _change(
            "aws_cloudwatch_log_group.cluster",
            actions,
            {
                "name": f"/aws/eks/{PREFIX}/cluster",
                **arn(
                    f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/eks/{PREFIX}/cluster"
                ),
            },
        ),
        _change(
            "aws_security_group.cluster",
            actions,
            {"name": f"{PREFIX}-cluster", **ident("sg-0cluster")},
        ),
        _change(
            "aws_vpc_security_group_egress_rule.cluster_all",
            actions,
            {
                **ident("sgr-0egress"),
                **({} if creating else {"security_group_id": "sg-0cluster"}),
            },
        ),
        _change(
            "aws_iam_role.cluster",
            actions,
            {
                "name": f"{PREFIX}-cluster-role",
                **arn(f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-cluster-role"),
            },
        ),
        _change(
            "aws_iam_role_policy_attachment.cluster_eks",
            actions,
            {"role": f"{PREFIX}-cluster-role"},
        ),
        _change(
            "aws_iam_role.node",
            actions,
            {
                "name": f"{PREFIX}-node-role",
                **arn(f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-node-role"),
            },
        ),
        _change(
            "aws_launch_template.node",
            actions,
            {
                "name": f"{PREFIX}-node",
                "id": "lt-test-root",
                "latest_version": 1,
                "block_device_mappings": [
                    {
                        "device_name": "/dev/xvda",
                        "ebs": [
                            {
                                "volume_size": 50,
                                "volume_type": "gp3",
                                "encrypted": "true",
                                "kms_key_id": TEST_KMS_KEY,
                            }
                        ],
                    }
                ],
            },
        ),
        _change(
            "aws_eks_cluster.workspace",
            actions,
            {
                "name": PREFIX,
                "version": STANDARD_VERSION,
                **arn(f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/{PREFIX}"),
            },
        ),
        _change(
            "aws_eks_node_group.default",
            actions,
            {
                "node_group_name": f"{PREFIX}-default",
                "cluster_name": PREFIX,
                "instance_types": ["m6i.large"],
                "scaling_config": [{"desired_size": 2, "min_size": 1, "max_size": 4}],
            },
        ),
        _change(
            "aws_iam_openid_connect_provider.cluster",
            actions,
            {
                **tags,
                **arn(
                    f"arn:aws:iam::{ACCOUNT}:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/ABC"
                ),
            },
        ),
    ]

    # The node role's three managed-policy attachments. `role` holds the role NAME, which is
    # directly comparable to the name `aws_iam_role.node` above was verified under.
    for attachment in ("node_worker",):
        changes.append(
            _change(
                f"aws_iam_role_policy_attachment.{attachment}",
                actions,
                {"role": f"{PREFIX}-node-role"},
            )
        )

    changes.extend(
        [
            _change(
                "aws_iam_role.vpc_cni",
                actions,
                {
                    "name": f"{PREFIX}-vpc-cni-role",
                    **arn(f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-vpc-cni-role"),
                },
            ),
            _change(
                "aws_iam_role_policy_attachment.vpc_cni",
                actions,
                {"role": f"{PREFIX}-vpc-cni-role"},
            ),
            _change(
                "aws_iam_role_policy.node_image_pull",
                actions,
                {"name": f"{PREFIX}-node-image-pull", "role": f"{PREFIX}-node-role"},
            ),
            _change(
                "aws_eks_addon.vpc_cni",
                actions,
                {
                    "addon_name": "vpc-cni",
                    "cluster_name": PREFIX,
                    **(
                        {}
                        if creating
                        else {
                            "service_account_role_arn": f"arn:aws:iam::{ACCOUNT}:role/{PREFIX}-vpc-cni-role"
                        }
                    ),
                },
            ),
        ]
    )

    if networking == "supplied":
        # network.tf gates every network resource on local.owns_network, so in supplied mode
        # none of them is in the plan at all — the VPC is READ, which is exactly what keeps it
        # out of this module's lifecycle (design item 3).
        changes.append(
            {
                "address": "data.aws_vpc.supplied",
                "change": {
                    "actions": ["read"],
                    "before": None,
                    "after": {"id": "vpc-0suppliedbyowner"},
                },
            }
        )
        return _genuine_network_context(_plan(*changes), creating, networking)

    changes += [
        _change(
            "aws_security_group.private_sts[0]",
            actions,
            {"name": f"{PREFIX}-private-sts", **tags, **ident("sg-0private-sts")},
        ),
        _change(
            "aws_vpc_endpoint.private_sts[0]",
            actions,
            {
                **tags,
                **ident("vpce-0private-sts"),
                "vpc_endpoint_type": "Interface",
                **(
                    {}
                    if creating
                    else {
                        "vpc_id": "vpc-0workspace",
                        "subnet_ids": ["subnet-0private0", "subnet-0private1"],
                        "security_group_ids": ["sg-0private-sts"],
                    }
                ),
            },
        ),
        _change(
            "aws_vpc_security_group_ingress_rule.private_sts_nodes[0]",
            actions,
            {
                **ident("sgr-0private-sts"),
                "ip_protocol": "tcp",
                "from_port": 443,
                "to_port": 443,
                **(
                    {}
                    if creating
                    else {
                        "security_group_id": "sg-0private-sts",
                        "referenced_security_group_id": "sg-0123456789abcdef0",
                    }
                ),
            },
        ),
        _change("aws_vpc.workspace[0]", actions, {**tags, **ident("vpc-0workspace")}),
        _change(
            "aws_internet_gateway.workspace[0]", actions, {**tags, **ident("igw-0ws")}
        ),
        _change("aws_eip.nat[0]", actions, {**tags, **ident("eipalloc-0ws")}),
        _change("aws_nat_gateway.workspace[0]", actions, {**tags, **ident("nat-0ws")}),
        _change("aws_route_table.public[0]", actions, {**tags, **ident("rtb-0public")}),
        _change(
            "aws_route_table.private[0]", actions, {**tags, **ident("rtb-0private")}
        ),
    ]
    for index in range(2):
        for tier in ("public", "private"):
            changes.append(
                _change(
                    f"aws_subnet.{tier}[{index}]",
                    actions,
                    {**tags, **ident(f"subnet-0{tier}{index}")},
                )
            )
            changes.append(
                _change(
                    f"aws_route_table_association.{tier}[{index}]",
                    actions,
                    {}
                    if creating
                    else {
                        "subnet_id": f"subnet-0{tier}{index}",
                        "route_table_id": f"rtb-0{tier}",
                    },
                )
            )
    return _genuine_network_context(_plan(*changes), creating, networking)


def test_the_genuine_plan_covers_every_declaration_the_module_makes() -> None:
    """The premise for the two positive tests below: the fixture is a WHOLE plan.

    A positive fixture that had quietly stopped representing a real plan — because a resource
    was added to the module and not to it — would keep passing while covering less and less.
    That is the failure mode that made the `84e3f7ee` review's derived-fixture rule necessary,
    so the coverage is asserted against the module's own source rather than trusted.
    """
    from workspace_ownership import declared_addresses, leaf_type_and_name

    covered = {
        leaf_type_and_name(change["address"])
        for change in _genuine_plan(creating=True)["resource_changes"]
    }
    declared = set(declared_addresses())
    # The admin role and its inline policy are absent on purpose: both are counted
    # (`local.workspace_admin_enabled`), and the default workspace names no operator, so a
    # genuine default plan does not contain them. Every other declaration must be present.
    optional = {
        ("aws_iam_role", "workspace_admin"),
        ("aws_iam_role_policy", "workspace_admin"),
    }
    missing = sorted(declared - covered - optional)
    assert not missing, (
        f"_genuine_plan() no longer represents a full plan: {missing} are declared in the "
        f"module's .tf source but absent from the fixture. Add them, or the two positive "
        f"tests below stop proving that the W9-03 rules accept a real deploy."
    )


def test_a_genuine_full_first_apply_is_approved(tmp_path) -> None:
    """Every relationship target is UNKNOWN here, and that must not be a denial.

    On a first apply `subnet_id`, `route_table_id` and `security_group_id` are all references
    to ids AWS has not assigned yet. Refusing an unresolvable target unconditionally would be
    the easy way to make the W9-03 third reproduction fail — and would also deny every real
    deploy, which is why the rule distinguishes unknown-on-create from unknown-on-destroy.
    """
    result = _run(_write(tmp_path, _genuine_plan(creating=True)))
    assert result.returncode == 0, (
        f"the guard denied a complete, correctly-named create plan for this workspace. The "
        f"W9-03 repair must not be satisfiable by refusing everything.\n"
        f"Output:\n{result.stdout}{result.stderr}"
    )
    assert "No destructive change" in result.stdout


def test_a_genuine_full_teardown_with_exact_authorization_is_approved(tmp_path) -> None:
    """The case that exercises owned-ID resolution, and the one a teardown lane depends on.

    Here every id is known, so each association must resolve to a subnet and a route table this
    same plan verified as owned, and the egress rule to this workspace's own security group. It
    is also the destructive path, so `--authorize-destroy` must bind this exact plan.

    This is the realistic end-to-end shape of the whole mechanism: a 30-address teardown,
    authorized by the document the guard emits for it, approved. If the W9-04 bindings were too
    strict to authorize a real teardown, this is the test that would say so.
    """
    plan = _genuine_plan(creating=False)
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    assert result.returncode == 0, (
        f"the guard denied a complete teardown of this workspace's own resources with an "
        f"exactly matching authorization. A teardown lane that cannot be authorized is a "
        f"teardown done by hand.\nOutput:\n{result.stdout}{result.stderr}"
    )
    assert "Authorization is bound to this exact plan" in result.stdout


def test_a_genuine_supplied_networking_apply_is_approved(tmp_path) -> None:
    """Supplied mode: no network resources at all, and the supplied VPC only read.

    Covered separately because the mode changes which addresses legitimately appear. A rule
    tuned to owned mode's shape — for instance one that required an owned VPC id to exist
    before accepting anything — would reject this and nothing else here would notice.
    """
    result = _run(_write(tmp_path, _genuine_plan(creating=True, networking="supplied")))
    assert result.returncode == 0, (
        f"the guard denied a legitimate supplied-networking apply.\n"
        f"Output:\n{result.stdout}{result.stderr}"
    )


def test_a_workspace_whose_name_prefixes_this_ones_is_denied(tmp_path) -> None:
    """THE W9-03 CONTROL, reproduction 1 — accepted on the reviewed head.

    Workspace `alpha`'s prefix is `adp-dev-spw-alpha`, and the pattern this replaced was
    `^adp-dev-spw-alpha(-[a-z0-9][a-z0-9-]*)?$`. `adp-dev-spw-alpha-prod` matches it — and is
    workspace `alpha-prod`'s cluster, in the same account. The guard's own docstring claimed
    "two workspaces in one account differ only in this segment" while the pattern was blind to
    precisely that segment whenever one workspace's name is a prefix of another's.

    Run with `--workspace-name alpha` rather than the module-level constant, because the whole
    defect depends on the victim workspace's name being a prefix of the attacker's. Authorized,
    so ownership is the only gate left — see `_authorize`.
    """
    change = _change(
        "aws_eks_cluster.workspace",
        ["delete"],
        {
            "name": "adp-dev-spw-alpha-prod",
            "arn": f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/adp-dev-spw-alpha-prod",
        },
    )
    plan = _plan(change)
    result = _run(
        _write(tmp_path, plan), *_authorize(tmp_path, plan), workspace="alpha"
    )
    _assert_denied(result, because="is not the name this declaration produces")
    assert "PREFIX" in result.stdout, (
        f"the denial must say WHY this is an equality test rather than a prefix match, or the "
        f"next author restores the prefix pattern for being more permissive.\n"
        f"Output:\n{result.stdout}"
    )


def test_a_tagged_resource_from_another_environment_is_denied(tmp_path) -> None:
    """THE W9-03 CONTROL, reproduction 2 — accepted on the reviewed head.

    A VPC carrying the RIGHT workspace tag and the WRONG environment tag. Only `Workspace` was
    compared, and a tag-identified resource's environment appears in no name, so nothing in the
    guard looked at `Environment` at all: a plan for `dev` would delete `prod`'s VPC. One
    workspace name can exist in several environments, and they are different blast radii.
    """
    plan = _plan(_vpc(["delete"], environment="prod"))
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    _assert_denied(result, because="is tagged Environment='prod'")


def test_an_attachment_pointing_at_a_foreign_role_is_denied(tmp_path) -> None:
    """THE W9-03 CONTROL, reproduction 3 — accepted on the reviewed head.

    `aws_iam_role_policy_attachment.node_worker` IS a declaration this module makes, so the
    address check passed — and the address is a label this module's source chooses, not a fact
    about AWS. Its `role` named `unrelated-production-node-role`, and destroying the attachment
    detaches a policy from that foreign role: a live permission change to something this
    workspace does not own.
    """
    change = _change(
        "aws_iam_role_policy_attachment.node_worker",
        ["delete"],
        {
            "role": "unrelated-production-node-role",
            "policy_arn": "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
        },
    )
    plan = _plan(change)
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    # The reason names the REQUIRED KIND, not just "some resource". Review follow-up 1 was that
    # the owned set was untyped, so this wording is part of what is being pinned.
    _assert_denied(
        result, because="is not the name of any aws_iam_role this plan verified"
    )


def test_an_association_pointing_at_a_foreign_subnet_is_denied(tmp_path) -> None:
    """The same defect on the id-resolved side of RELATIONSHIP_TARGET_FIELDS.

    A route table association names two AWS-assigned ids. Destroying one that points at another
    tenant's subnet detaches that subnet from its route table, which removes its egress — and
    nothing about the Terraform address says whose subnet it is.
    """
    plan = _plan(
        _change(
            "aws_route_table.private[0]",
            ["delete"],
            {"tags_all": _owned_tags(), "id": "rtb-0private"},
        ),
        _change(
            "aws_route_table_association.private[0]",
            ["delete"],
            {"subnet_id": "subnet-0somebodyelses", "route_table_id": "rtb-0private"},
        ),
    )
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    _assert_denied(result, because="is not the id of any aws_subnet this plan verified")
    assert "subnet-0somebodyelses" in result.stdout


def test_a_destructive_attachment_with_an_unresolvable_target_is_denied(
    tmp_path,
) -> None:
    """Unknown is not foreign, but on a destroy it is not "ours" either.

    An absent or unknown target is normal on a create — the id does not exist yet — and
    `test_a_genuine_full_first_apply_is_approved` depends on that being tolerated. A destroy
    acts on something that ALREADY exists, so its identity must be established before it is
    removed; "I could not tell whose this is" must not resolve to "proceed" for the one class
    of change that cannot be undone.
    """
    change = _change(
        "aws_vpc_security_group_egress_rule.cluster_all",
        ["delete"],
        {"id": "sgr-0egress"},
    )
    plan = _plan(change)
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    _assert_denied(result, because="cannot be resolved")


def test_a_foreign_resource_cannot_launder_an_attachment_that_points_at_it(
    tmp_path,
) -> None:
    """One violating resource must not become the justification for accepting its dependents.

    NOT one of the three reproductions: the pre-repair guard did DENY this plan, because the
    foreign role failed its own name check. What it did not do is name the attachment, and this
    test asserts that second part.

    It guards the repair rather than the original defect. If `_owned_identifiers` built the
    owned set from every resource in the plan instead of only the violation-free ones, a plan
    could smuggle in a foreign role AND its attachment: the role would be flagged, the
    attachment would resolve happily against the very name that was flagged, and an operator
    skimming the output would see one violation where there are two. That is a plausible
    simplification of the repair, so it is pinned.
    """
    plan = _plan(
        _change(
            "aws_iam_role.node",
            ["delete"],
            {
                "name": "unrelated-production-node-role",
                "arn": f"arn:aws:iam::{ACCOUNT}:role/unrelated-production-node-role",
            },
        ),
        _change(
            "aws_iam_role_policy_attachment.node_worker",
            ["delete"],
            {"role": "unrelated-production-node-role"},
        ),
    )
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    _assert_denied(
        result, because="is not the name of any aws_iam_role this plan verified"
    )
    assert "aws_iam_role_policy_attachment.node_worker" in result.stdout, (
        f"the attachment itself must be named as a violation, not merely implied by the role's. "
        f"Output:\n{result.stdout}"
    )


# ===========================================================================
# W9-03 FOLLOW-UPS: the three cases that still reproduced on attempt 3's head
# ===========================================================================
# The three controls above cover the ORIGINAL reproductions. The review of attempt 3 found that
# the repair for the third one was still defeatable three ways, and each was reproduced against
# that head before being repaired here:
#
#   1. The owned set was UNTYPED. `_owned_identifiers` pooled every verified name into one set,
#      so an owned EKS CLUSTER named `adp-dev-spw-alpha` satisfied an attachment whose `role`
#      was `adp-dev-spw-alpha` — an IAM role of that name this module never declares. Repaired
#      by indexing the owned names and ids BY RESOURCE TYPE and giving each target field the
#      resource types that may legitimately satisfy it (`RELATIONSHIP_TARGET_FIELDS`).
#   2. `aws_iam_role_policy` — an INLINE policy — was absent from RELATIONSHIP_TARGET_FIELDS
#      entirely. It is name-identified, so its own `name` was checked and its `role` never was:
#      a correctly-named inline policy could name any role in the account, and deleting it
#      removes permissions from that foreign role. Repaired by giving it the same typed `role`
#      target as the attachment.
#   3. Identity was pooled ACROSS SIDES. `validate_identity` required a tag (or a name) on at
#      least ONE side, so a REPLACEMENT with correct tags on `after` and none on `before`
#      passed — and the `before` is the object that gets destroyed. Worse, the unattributed
#      `before.id` was then published by `_owned_identifiers` as a verified owned id, so it
#      could vouch for the associations pointing at it. Repaired by attributing each present
#      side independently (`_present_sides`).
#
# Each is pinned separately because they had three different causes in two different functions.
# The positive tests above (`test_a_genuine_full_first_apply_is_approved`,
# `..._full_teardown_...`, `..._supplied_networking_...`) remain the guard against a repair that
# just refuses more, and they still pass — a genuine create has no `before` at all and a genuine
# teardown has no `after`, so the per-side rule costs a real deploy nothing.
# ---------------------------------------------------------------------------


def test_an_owned_name_of_another_kind_cannot_vouch_for_a_role(tmp_path) -> None:
    """FOLLOW-UP 1 — accepted on attempt 3's head, which printed 0 violations.

    The plan holds a genuinely owned EKS cluster named `adp-dev-spw-alpha`, and an attachment
    whose `role` is the SAME STRING. There is no IAM role of that name here — this module names
    its roles `<prefix>-cluster-role` and `<prefix>-node-role`, never the bare prefix — so the
    attachment points at something this plan never verified. With one pooled set of owned names,
    the cluster's name answered for the role.

    A name identifies a resource only together with its kind. `alpha` is run as the workspace
    name because the bare prefix IS the cluster's name, which is what makes the collision
    expressible at all — so the plan's own `variables` must say `alpha` too, or the guard refuses
    it earlier for a target its flags and its plan disagree about.
    """
    plan = _plan(
        workspace="alpha",
        *_changes_for_cross_kind_collision(),
    )
    result = _run(
        _write(tmp_path, plan),
        *_authorize(tmp_path, plan, workspace="alpha"),
        workspace="alpha",
    )
    _assert_denied(
        result, because="is not the name of any aws_iam_role this plan verified"
    )
    assert "another kind does not confer ownership" in result.stdout, (
        f"a cross-kind collision looks like a correct name to whoever wrote the plan, so the "
        f"denial must say that a same-spelled object of another KIND does not vouch for it — "
        f"otherwise the reader concludes the guard is simply wrong and loosens it.\n"
        f"Output:\n{result.stdout}"
    )


def _changes_for_cross_kind_collision() -> tuple[dict, ...]:
    return (
        _change(
            "aws_eks_cluster.workspace",
            ["update"],
            {
                "name": name_prefix("dev", "alpha"),
                "arn": f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/adp-dev-spw-alpha",
                "version": STANDARD_VERSION,
            },
        ),
        _change(
            "aws_iam_role_policy_attachment.node_worker",
            ["delete"],
            {
                "role": name_prefix("dev", "alpha"),
                "policy_arn": "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
            },
        ),
    )


def test_an_inline_role_policy_naming_a_foreign_role_is_denied(tmp_path) -> None:
    """FOLLOW-UP 2 — accepted on attempt 3's head, which printed 0 violations.

    `aws_iam_role_policy.workspace_admin` is name-identified, so its own `name` was checked and
    matched. Its `role` was never checked at all, because the type was missing from
    RELATIONSHIP_TARGET_FIELDS. Deleting an inline policy from a role removes that role's
    permissions — the same live permission change as the managed-policy attachment case, on a
    type the first repair did not cover.

    Its own name is deliberately CORRECT here. A fixture with a wrong name would be denied by
    the name check and would never reach the target check this test exists for.
    """
    plan = _plan(
        _change(
            "aws_iam_role_policy.workspace_admin[0]",
            ["delete"],
            {
                "name": f"{PREFIX}-admin-eks-access",
                "role": "unrelated-production-node-role",
            },
        )
    )
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    _assert_denied(
        result, because="is not the name of any aws_iam_role this plan verified"
    )
    assert "aws_iam_role_policy.workspace_admin[0]" in result.stdout


def test_a_replacement_cannot_borrow_the_new_resources_tags(tmp_path) -> None:
    """FOLLOW-UP 3 — accepted on attempt 3's head, which printed 0 violations.

    A `["delete", "create"]` replacement of the VPC. The `after` carries this workspace's
    identity tags; the `before` — the object that actually gets DESTROYED — carries none. Under
    the "at least one side must carry the tag" rule the after side answered for the before side,
    so tagging the new resource retroactively authorized destroying an unattributed old one.

    A replacement rather than a plain delete on purpose: a delete has no `after` to borrow from,
    so the defect is only expressible on a change that has both sides.
    """
    plan = _plan(
        {
            "address": "aws_vpc.workspace[0]",
            "change": {
                "actions": ["delete", "create"],
                "before": {"id": "vpc-0notattributedtoanybody", "tags_all": {}},
                "after": {"cidr_block": "10.64.0.0/16", "tags_all": _owned_tags()},
            },
        }
    )
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    _assert_denied(result, because="carries no `WorkspaceId` tag on its before side")
    assert "DIFFERENT" in result.stdout, (
        f"the denial must say the two sides are different AWS objects, or the next author "
        f"restores the pooled rule for accepting a plan that 'obviously' has the right tags.\n"
        f"Output:\n{result.stdout}"
    )


def test_a_replacement_cannot_borrow_the_new_resources_name(tmp_path) -> None:
    """FOLLOW-UP 3 on the NAME-identified side. Separate branch, same defect.

    `validate_identity` has two independent attribution branches, and the pooled-sides rule was
    in both. Fixing only the tag branch would leave a replacement of the cluster able to destroy
    an unnamed `before` on the strength of a correctly-named `after`.
    """
    plan = _plan(
        {
            "address": "aws_eks_cluster.workspace",
            "change": {
                "actions": ["delete", "create"],
                "before": {"id": "adp-dev-spw-somebody-else"},
                "after": {
                    "name": PREFIX,
                    "arn": f"arn:aws:eks:us-east-1:{ACCOUNT}:cluster/{PREFIX}",
                    "version": STANDARD_VERSION,
                },
            },
        }
    )
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    _assert_denied(result, because="its before side carries no 'name'")


def test_an_unattributed_before_side_id_cannot_vouch_for_an_association(
    tmp_path,
) -> None:
    """FOLLOW-UP 3's second half: the harvesting side of the same pooling defect.

    Even once a replacement is denied, the question remains what `_owned_identifiers` PUBLISHED
    from it. On attempt 3's head the untagged `before` of a tag-borrowing replacement was
    accepted, and its id went into the owned set — so a route table association pointing at that
    foreign subnet resolved cleanly and was approved.

    The subnet here replaces with a tagged `after` and an untagged `before` holding
    `subnet-0somebodyelses`, and the association points at exactly that id. Both must be denied:
    the subnet for its unattributed before side, and the association for pointing at an id this
    plan never verified. Asserting only the first would leave the laundering path uncovered.
    """
    plan = _plan(
        _change(
            "aws_route_table.private[0]",
            ["delete"],
            {"tags_all": _owned_tags(), "id": "rtb-0private"},
        ),
        {
            "address": "aws_subnet.private[0]",
            "change": {
                "actions": ["delete", "create"],
                "before": {"id": "subnet-0somebodyelses", "tags_all": {}},
                "after": {"tags_all": _owned_tags()},
            },
        },
        _change(
            "aws_route_table_association.private[0]",
            ["delete"],
            {
                "subnet_id": "subnet-0somebodyelses",
                "route_table_id": "rtb-0private",
            },
        ),
    )
    result = _run(_write(tmp_path, plan), *_authorize(tmp_path, plan))
    _assert_denied(result, because="is not the id of any aws_subnet this plan verified")
    combined = result.stdout + result.stderr
    assert "aws_subnet.private[0]" in combined, (
        f"the subnet's own unattributed before side must also be reported. If only the "
        f"association is named, the next author 'fixes' the association and leaves the "
        f"laundering route open.\nOutput:\n{combined}"
    )
    assert "subnet-0somebodyelses" not in _owned_ids_for(plan).get(
        "aws_subnet", set()
    ), (
        "the unattributed before-side id was published as a verified owned subnet id. That is "
        "the laundering step itself: once it is in the owned set, every association pointing at "
        "that foreign subnet resolves cleanly."
    )


def _owned_ids_for(plan: dict) -> dict:
    """The owned-id index the guard builds for `plan`, for direct assertion.

    Called in-process rather than inferred from the CLI output because the test above asserts an
    ABSENCE, and an absence in a human-readable report is exactly what a formatting change could
    make vacuous.
    """
    from workspace_ownership import _owned_identifiers

    _names, ids = _owned_identifiers(
        plan["resource_changes"], ENVIRONMENT, WORKSPACE, ACCOUNT, ORG_ID, WORKSPACE
    )
    return ids


# ---------------------------------------------------------------------------
# Ownership CHANGE: the adoption design item 3 forbids
# ---------------------------------------------------------------------------
def test_importing_a_network_resource_is_denied(tmp_path) -> None:
    """An import is not a delete, so a destructive-change guard alone passes it.

    It is the route into adoption that defeats the owned-mode count gate, because an imported
    resource is a MANAGED resource: the next `terraform destroy` deletes the supplier's VPC.
    """
    change = _vpc(["no-op"])
    change["change"]["importing"] = {"id": "vpc-0supplied1234567"}
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="is being IMPORTED")


def test_flipping_the_network_ownership_tag_is_denied(tmp_path) -> None:
    """What switching networking_mode on a live workspace looks like from the plan's side.

    Also not a delete — the VPC is updated in place, and its lifecycle owner changes.
    """
    change = _vpc(["update"])
    change["change"]["before"] = {
        "cidr_block": "10.64.0.0/16",
        "tags_all": {"Workspace": WORKSPACE, "NetworkOwnership": "supplied"},
    }
    change["change"]["after"] = {
        "cidr_block": "10.64.0.0/16",
        "tags_all": {"Workspace": WORKSPACE, "NetworkOwnership": "adp-created"},
    }
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="Networking ownership is not an in-place update")


def test_an_unrecognised_network_ownership_value_is_denied(tmp_path) -> None:
    """The tag answers 'did ADP create this?'. An unknown value answers it wrongly."""
    change = _vpc(["create"], ownership="maybe")
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="which is not one of")


# ---------------------------------------------------------------------------
# Malformed input: the fail-open shapes this replaces
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("", "is empty"),
        ("not json at all", "is not valid JSON"),
        # Accepted as an empty plan by the weaker "either key is enough" rule the domain
        # guard uses. Caught by this test; the guard now requires format_version outright.
        ('{"resource_changes": []}', "no 'format_version'"),
        ("[]", "plan JSON is not an object"),
        # The `84e3f7ee` reproduction: `plan.get("resource_changes") or []` coerced every
        # falsy wrong type into an accepted empty list, and the CLI printed
        # "Validated 0 resource change(s)" and exited 0.
        ('{"format_version":"1.2","resource_changes":false}', "must be a list"),
        ('{"format_version":"1.2","resource_changes":0}', "must be a list"),
        ('{"format_version":"1.2","resource_changes":{}}', "must be a list"),
        ('{"format_version":"1.2","resource_changes":""}', "must be a list"),
    ],
    ids=[
        "empty-file",
        "not-json",
        "no-format-version",
        "top-level-list",
        "changes-false",
        "changes-zero",
        "changes-object",
        "changes-empty-string",
    ],
)
def test_a_malformed_plan_denies(tmp_path, body: str, reason: str) -> None:
    path = tmp_path / "plan.json"
    path.write_text(body, encoding="utf-8")
    _assert_denied(_run(path), because=reason)


def test_a_missing_plan_file_denies(tmp_path) -> None:
    _assert_denied(_run(tmp_path / "absent.json"), because="could not read plan JSON")


def test_an_unrecognised_action_word_denies(tmp_path) -> None:
    """A misspelled `"destroy"` must not be reported as a safe change.

    It would fail to intersect DESTRUCTIVE_ACTIONS and pass as harmless. Deletion is detected
    by RECOGNISING the vocabulary, not by failing to recognise it.
    """
    change = _cluster(["create"])
    change["change"]["actions"] = ["destroy"]
    result = _run(_write(tmp_path, _plan(change)))
    _assert_denied(result, because="unrecognised action(s)")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"change": {"actions": ["create"]}}, "has no address"),
        ({"address": "aws_vpc.workspace[0]"}, "'change' is missing or not an object"),
        (
            {"address": "aws_vpc.workspace[0]", "change": {"actions": []}},
            "'change.actions' is missing or not a list",
        ),
        (
            {"address": "aws_vpc.workspace[0]", "change": {"actions": "create"}},
            "'change.actions' is missing or not a list",
        ),
    ],
    ids=["no-address", "no-change", "empty-actions", "actions-not-a-list"],
)
def test_a_truncated_change_entry_denies(tmp_path, change: dict, reason: str) -> None:
    """A hand-edited or truncated plan denies rather than being partially interpreted."""
    _assert_denied(_run(_write(tmp_path, _plan(change))), because=reason)


def test_a_malformed_drift_entry_denies(tmp_path) -> None:
    plan = _plan(_cluster(["create"]), drift=[{"no_address": True}])
    _assert_denied(
        _run(_write(tmp_path, plan)), because="resource_drift entry is malformed"
    )


# ---------------------------------------------------------------------------
# Change inventory (design item 4)
# ---------------------------------------------------------------------------
def test_the_inventory_records_every_change_deterministically(tmp_path) -> None:
    plan = _plan(_node_group(), _cluster(["create"]), _vpc(["delete", "create"]))
    inventory_path = tmp_path / "inventory.json"

    result = _run(
        _write(tmp_path, plan),
        "--inventory",
        str(inventory_path),
        *_authorize(tmp_path, plan),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    inventory = json.loads(inventory_path.read_text())
    assert inventory["total_changes"] == 7
    assert inventory["workspace_name"] == WORKSPACE
    assert inventory["destructive_addresses"] == ["aws_vpc.workspace[0]"]
    assert inventory["counts_by_type"] == {
        "aws_eks_cluster": 1,
        "aws_eks_node_group": 1,
        "aws_launch_template": 1,
        "aws_vpc": 1,
        "aws_iam_role": 2,
        "aws_security_group": 1,
    }

    addresses = [entry["address"] for entry in inventory["changes"]]
    assert addresses == sorted(addresses), (
        "the inventory must be sorted by address. Terraform's ordering is stable in practice "
        "but not guaranteed, and an inventory whose line order varies between runs cannot be "
        "diffed to answer 'is this the plan that was reviewed?'."
    )


def test_the_inventory_is_written_even_when_the_plan_is_denied(tmp_path) -> None:
    """A denied plan is exactly the one whose inventory a reviewer needs to read."""
    plan = _plan(_cluster(["delete"]))
    inventory_path = tmp_path / "inventory.json"
    result = _run(_write(tmp_path, plan), "--inventory", str(inventory_path))

    assert result.returncode != 0
    assert inventory_path.exists(), (
        "the inventory must be written before the verdict. Withholding the evidence on "
        "denial removes it at the moment it is wanted."
    )
    assert json.loads(inventory_path.read_text())["destructive_addresses"] == [
        "aws_eks_cluster.workspace"
    ]


# ---------------------------------------------------------------------------
# Bounded cost/resource estimate (design item 4)
# ---------------------------------------------------------------------------
def test_the_estimate_bounds_cost_at_the_node_ceiling(tmp_path) -> None:
    """The bound is computed at max_size, never desired_size.

    The figure a reviewer needs is what this workspace can cost at its reviewed ceiling; a
    desired-size number understates it by the whole point of having a ceiling.
    """
    plan = _plan(_cluster(["create"]), _vpc(["create"]), _node_group(max_size=2))
    estimate_path = tmp_path / "estimate.json"
    result = _run(_write(tmp_path, plan), "--estimate", str(estimate_path))
    assert result.returncode == 0, result.stdout + result.stderr

    estimate = json.loads(estimate_path.read_text())
    assert estimate["is_upper_bound"] is True

    components = {
        line["component"]: line["monthly_usd"] for line in estimate["components"]
    }
    node_line = next(
        key for key in components if key.startswith("Node group compute at its ceiling")
    )
    # 2 nodes x $0.096/hour x 730 hours. Asserted as a computed expectation rather than a
    # literal, so a rate-table correction moves the expectation with it instead of failing.
    assert components[node_line] == pytest.approx(0.096 * 2 * 730, abs=0.01)
    control_plane = next(
        key for key in components if key.startswith("EKS control plane")
    )
    assert components[control_plane] == pytest.approx(0.10 * 730, abs=0.01), (
        "a standard-support cluster must be priced at the standard rate. If this is 6x too "
        "high the support tier is being read as extended."
    )
    assert estimate["bounded_monthly_usd"] == pytest.approx(
        sum(components.values()), abs=0.01
    )


def test_the_estimate_scales_with_the_ceiling_not_the_desired_size(tmp_path) -> None:
    """Anti-vacuous: the previous test would pass against a hardcoded number.

    Raising max_size must raise the bound proportionally. A guard reading desired_size — which
    is 1 in both fixtures — would return the same figure for both and pass the test above.
    """
    small = tmp_path / "small.json"
    large = tmp_path / "large.json"

    _run(
        _write(tmp_path, _plan(_node_group(max_size=2)), "p1.json"),
        "--estimate",
        str(small),
    )
    _run(
        _write(tmp_path, _plan(_node_group(max_size=10)), "p2.json"),
        "--estimate",
        str(large),
    )

    small_total = json.loads(small.read_text())["bounded_monthly_usd"]
    large_total = json.loads(large.read_text())["bounded_monthly_usd"]
    control_plane = CONTROL_PLANE_HOURLY_USD["standard"] * HOURS_PER_MONTH
    assert large_total - control_plane == pytest.approx(
        (small_total - control_plane) * 5, rel=0.01
    ), (
        f"a 5x larger ceiling must produce a 5x larger bound (got {small_total} and "
        f"{large_total}). If these are equal, the estimate is reading desired_size — which is "
        f"1 in both fixtures — and reports no ceiling at all."
    )


def test_an_unpriced_instance_type_denies(tmp_path) -> None:
    """A silent zero would report a reassuringly small bound for GPU capacity.

    The specific hazard: #5533 (w6-10) brings GPU nodes to this cluster, and an accelerated
    instance costs two orders of magnitude more than the general-purpose default.
    """
    plan = _plan(_node_group(instance_types=["p5.48xlarge"]))
    result = _run(_write(tmp_path, plan), "--estimate", str(tmp_path / "estimate.json"))
    _assert_denied(result, because="not in this guard's rate table")


def test_a_node_group_with_no_ceiling_denies(tmp_path) -> None:
    """An unbounded node group must not be reported as costing nothing."""
    plan = _plan(_node_group(max_size=None))
    result = _run(_write(tmp_path, plan), "--estimate", str(tmp_path / "estimate.json"))
    _assert_denied(result, because="gives no finite ceiling")


def test_the_estimate_names_what_it_does_not_bound(tmp_path) -> None:
    """An estimate that omits its gaps reads as complete."""
    estimate_path = tmp_path / "estimate.json"
    _run(
        _write(tmp_path, _plan(_cluster(["create"]))),
        "--estimate",
        str(estimate_path),
    )
    unbounded = json.loads(estimate_path.read_text())["not_bounded_by_this_estimate"]
    assert len(unbounded) >= 4, (
        "the estimate must name the usage-driven components it cannot bound from a plan "
        "document. Omitting them would present a partial figure as a total."
    )
    assert any("GPU" in item for item in unbounded), (
        "the GPU exclusion must be stated: this module creates a general-purpose group only, "
        "and the bound is for an EMPTY workspace. #5533 (w6-10) is what changes that."
    )


def test_a_destroy_plan_is_not_reported_as_a_cost_increase(tmp_path) -> None:
    """A pure delete leaves nothing to charge for, and W9-05 requires that to be EXPLICIT.

    Kept from before the W9-05 repair, because this is the one thing the old
    `"create" in actions` filter got right, and the repair must not fix the update case by
    starting to price teardowns. The added half is `not_priced_because_removed_or_read`: $0.00
    now has to come with the reason, so "why is this teardown free" is answerable from the
    artifact rather than from the source.
    """
    plan = _plan(_cluster(["delete"]), _vpc(["delete"]))
    estimate_path = tmp_path / "estimate.json"
    result = _run(
        _write(tmp_path, plan),
        "--estimate",
        str(estimate_path),
        *_authorize(tmp_path, plan),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    estimate = json.loads(estimate_path.read_text())
    assert estimate["bounded_monthly_usd"] == 0.0
    assert estimate["components"] == []
    skipped = estimate["not_priced_because_removed_or_read"]
    assert len(skipped) == 2 and all("delete" in entry for entry in skipped), (
        f"a $0.00 bound must name the addresses it did not price and why. Silently dropping "
        f"them makes a teardown indistinguishable from a guard that priced nothing.\n"
        f"Got: {skipped}"
    )


# ===========================================================================
# REVIEW FINDING W9-05: the bound priced only `create`, so most plans came out $0.00
# ===========================================================================
# The pre-repair filter was `if "create" not in actions: continue`, with the rationale that an
# update's charge was "already counted in the previous estimate" — a previous estimate that
# nothing in this lane produces or compares against.
#
# MEASURED against the guard at 0ec565d5, driven with the same fixtures as below:
#
#   *  an UPDATE raising the node ceiling 2 -> 40 and the type to m6i.2xlarge — the plan whose
#      entire purpose is to change capacity — priced at **$0.00**. Correct bound: ~$11,213/mo.
#   *  a NO-OP re-plan of an existing workspace priced at **$0.00**, so the answer to "what does
#      this workspace cost" was zero for every workspace that already existed. Correct: ~$774/mo.
#   *  a cluster on Kubernetes 1.31 priced at **$73/mo**. 1.31 is in EKS EXTENDED support, which
#      AWS bills at $0.60/hour rather than $0.10 — the bound was **6x too low** while the
#      artifact said `is_upper_bound: true`.
#   *  a cluster on RETIRED 1.27 was priced and approved, though EKS will not create it.
#   *  `--aws-region` was not an input at all. Every price was a us-east-1 price whatever the
#      workspace's region, and the artifact recorded `rate_table_region` — the TABLE's region —
#      where a reader would look for the workspace's.
#
# Two sub-cases already behaved correctly and are asserted rather than claimed as repairs: a
# REPLACEMENT was priced (`["delete","create"]` contains `create`, so it passed the old filter
# incidentally), and an unknown `max_size` already refused.
#
# Every expectation below is COMPUTED from the guard's own rate constants, not written as a
# literal, so a published-price correction moves the expectation with it instead of failing.
# ---------------------------------------------------------------------------
def test_an_update_that_raises_the_ceiling_is_priced(tmp_path) -> None:
    """W9-05's primary case: the capacity-change plan that priced at $0.00.

    A node group going from 2x m6i.large to 40x m6i.2xlarge is an `["update"]` — nothing is
    created — so the old filter skipped it entirely. This is the single plan shape whose whole
    reason for existing is to change what the workspace can cost.
    """
    plan = _plan(
        _node_group(("update",), instance_types=["m6i.2xlarge"], max_size=40),
    )
    estimate_path = tmp_path / "estimate.json"
    result = _run(_write(tmp_path, plan), "--estimate", str(estimate_path))
    assert result.returncode == 0, result.stdout + result.stderr

    estimate = json.loads(estimate_path.read_text())
    expected = (
        INSTANCE_HOURLY_USD["m6i.2xlarge"] * 40 * HOURS_PER_MONTH
        + 40 * 50 * EBS_GP3_MONTHLY_USD_PER_GIB
        + CONTROL_PLANE_HOURLY_USD["standard"] * HOURS_PER_MONTH
    )
    assert estimate["bounded_monthly_usd"] == pytest.approx(expected, abs=0.01), (
        f"an update raising the ceiling to 40 m6i.2xlarge nodes must be priced at about "
        f"${expected:,.0f}/month. ${estimate['bounded_monthly_usd']:,.2f} — and $0.00 in "
        f"particular — is the W9-05 defect: 'already counted in the previous estimate' assumed "
        f"a baseline this lane does not have."
    )
    assert estimate["not_priced_because_removed_or_read"] == []


def test_a_no_op_replan_still_reports_what_the_workspace_costs(tmp_path) -> None:
    """The second W9-05 case: an existing workspace re-planned is all no-ops.

    "What does this workspace cost" must not answer $0.00 for every workspace that already
    exists. The bound is a property of the resulting capacity, not of the size of the diff.
    """
    plan = _plan(_cluster(["no-op"]), _node_group(("no-op",), max_size=10))
    estimate_path = tmp_path / "estimate.json"
    result = _run(_write(tmp_path, plan), "--estimate", str(estimate_path))
    assert result.returncode == 0, result.stdout + result.stderr

    expected = (
        CONTROL_PLANE_HOURLY_USD["standard"] * HOURS_PER_MONTH
        + INSTANCE_HOURLY_USD["m6i.large"] * 10 * HOURS_PER_MONTH
        + 10 * 50 * EBS_GP3_MONTHLY_USD_PER_GIB
    )
    assert json.loads(estimate_path.read_text())[
        "bounded_monthly_usd"
    ] == pytest.approx(expected, abs=0.01), (
        f"a no-op re-plan of a live workspace must still report about ${expected:,.0f}/month. "
        f"Pricing only the diff reports $0.00 for every workspace that already exists."
    )


def test_a_replacement_is_priced_because_the_resource_exists_afterwards(
    tmp_path,
) -> None:
    """Already correct pre-repair, and asserted so the fix cannot regress it.

    `["delete","create"]` passed the old `create` filter incidentally rather than by design.
    Under the new rule it is priced deliberately: a replacement ends with the resource in place.
    """
    plan = _plan(_node_group(("delete", "create"), max_size=10))
    estimate_path = tmp_path / "estimate.json"
    result = _run(
        _write(tmp_path, plan),
        "--estimate",
        str(estimate_path),
        *_authorize(tmp_path, plan),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    expected = (
        INSTANCE_HOURLY_USD["m6i.large"] * 10 * HOURS_PER_MONTH
        + 10 * 50 * EBS_GP3_MONTHLY_USD_PER_GIB
        + CONTROL_PLANE_HOURLY_USD["standard"] * HOURS_PER_MONTH
    )
    assert json.loads(estimate_path.read_text())[
        "bounded_monthly_usd"
    ] == pytest.approx(expected, abs=0.01)


def test_an_extended_support_cluster_is_priced_at_the_extended_rate(tmp_path) -> None:
    """W9-05's support-tier requirement, and a 6x understatement pre-repair.

    EKS bills $0.10/cluster/hour in standard support and $0.60 in extended. The old estimate
    had one flat 0.10 constant, so a cluster on an extended-support version was priced at one
    sixth of its cost — $73/month against $438 — while the artifact claimed `is_upper_bound`.
    A bound six times too low is worse than no bound, because it is believed.
    """
    standard = tmp_path / "standard.json"
    extended = tmp_path / "extended.json"
    _run(
        _write(tmp_path, _plan(_cluster(["create"])), "s.json"),
        "--estimate",
        str(standard),
    )
    result = _run(
        _write(
            tmp_path, _plan(_cluster(["create"], version=EXTENDED_VERSION)), "e.json"
        ),
        "--estimate",
        str(extended),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    got = json.loads(extended.read_text())["bounded_monthly_usd"]
    expected = CONTROL_PLANE_HOURLY_USD["extended"] * HOURS_PER_MONTH
    assert got == pytest.approx(expected, abs=0.01), (
        f"a cluster on Kubernetes {EXTENDED_VERSION} is in EKS extended support and must be "
        f"priced at ${expected:,.0f}/month, not ${got:,.2f}."
    )
    # The comparison, not just the figure: the ratio is what makes this a tier check rather
    # than an assertion that one number happens to equal another.
    ratio = got / json.loads(standard.read_text())["bounded_monthly_usd"]
    assert ratio == pytest.approx(6.0, abs=0.1), (
        f"extended support must cost 6x standard for the same cluster; got {ratio:.2f}x. If "
        f"this is 1.0 the tier is not being read at all."
    )
    assert "extended support" in str(json.loads(extended.read_text())["components"]), (
        "the component must SAY it is on extended support, or a reviewer sees a large number "
        "with no explanation and no action to take."
    )


@pytest.mark.parametrize(
    "region", ["us-west-2", "eu-west-1", "eu-central-1", "ap-southeast-2"]
)
def test_unverified_regional_prices_cannot_emit_a_bound(tmp_path, region) -> None:
    estimate = tmp_path / "estimate.json"
    result = _run(
        _write(
            tmp_path,
            _plan(
                _cluster(
                    ["create"], arn=f"arn:aws:eks:{region}:{ACCOUNT}:cluster/{PREFIX}"
                ),
                region=region,
            ),
        ),
        "--estimate",
        str(estimate),
        region=region,
    )
    _assert_denied(result, because="bounded pricing is supported only for us-east-1")
    assert not estimate.exists()


def test_an_estimate_without_a_region_is_refused(tmp_path) -> None:
    """A bound has to be a bound for SOMEWHERE. Defaulting to the base region is not that."""
    plan = _plan(_cluster(["create"]))
    result = _run(
        _write(tmp_path, plan),
        "--estimate",
        str(tmp_path / "estimate.json"),
        region=None,
    )
    _assert_denied(result, because="--aws-region is required")


def test_an_unknown_required_value_refuses_rather_than_pricing_zero(tmp_path) -> None:
    """W9-05: "refuse unknown required values".

    Terraform omits an attribute from `after` when it is not known until apply. For the node
    ceiling that is the difference between a bound and a fiction: `values.get("max_size") or 0`
    would turn an unknown ceiling into a $0.00 line, which is the fail-open direction — the
    reassuring number for the plan whose cost is least knowable.
    """
    plan = _plan(_node_group(max_size=None))
    result = _run(_write(tmp_path, plan), "--estimate", str(tmp_path / "estimate.json"))
    _assert_denied(result, because="gives no finite ceiling")


def test_the_estimate_is_refused_for_a_retired_kubernetes_version(tmp_path) -> None:
    """W9-05 and F6 meet here: an uncreateable version has no meaningful cost bound.

    Pre-repair a cluster pinned to retired 1.27 was priced at the standard rate and approved,
    though EKS will not create it. The apply fails — after the VPC and subnets exist.
    """
    plan = _plan(_cluster(["create"], version=RETIRED_VERSION))
    result = _run(_write(tmp_path, plan), "--estimate", str(tmp_path / "estimate.json"))
    _assert_denied(result, because="is RETIRED")


def test_node_root_volumes_are_priced_now_that_the_template_bounds_them(
    tmp_path,
) -> None:
    """The W9-02 launch template turned node storage from unbounded into a priced component.

    `UNBOUNDED_COMPONENTS` used to excuse node EBS on the grounds that the plan "does not set
    and therefore cannot bound" it. The launch template sets `volume_size`, so that sentence
    stopped being true and leaving it would have understated a 100-node workspace's storage by
    the whole of it. The volume COUNT is the node group's ceiling, which is why the line needs
    both resources in the plan.
    """
    size = 50
    plan = _plan(
        _node_group(max_size=10),
        _launch_template(volume_size=size),
    )
    estimate_path = tmp_path / "estimate.json"
    result = _run(_write(tmp_path, plan), "--estimate", str(estimate_path))
    assert result.returncode == 0, result.stdout + result.stderr

    estimate = json.loads(estimate_path.read_text())
    ebs = next(
        (
            line
            for line in estimate["components"]
            if line["component"].startswith("Node root EBS volumes")
        ),
        None,
    )
    assert ebs is not None, (
        f"node root volumes must appear as a priced component now that the launch template "
        f"sets their size.\nComponents: "
        f"{[line['component'] for line in estimate['components']]}"
    )
    assert ebs["monthly_usd"] == pytest.approx(
        EBS_GP3_MONTHLY_USD_PER_GIB * size * 10, abs=0.01
    )
    assert not any(
        "does not set and therefore cannot bound" in item
        for item in estimate["not_bounded_by_this_estimate"]
    ), (
        "the stale exclusion claiming the plan cannot bound node EBS must be gone: the launch "
        "template sets the size, and an exclusion that is no longer true understates the total "
        "while reading as rigour."
    )


# ---------------------------------------------------------------------------
# The two policies stay in step
# ---------------------------------------------------------------------------
def test_the_guards_network_type_list_matches_the_source_level_suite() -> None:
    """One category of network-owning types, enforced in two places.

    `test_networking_modes.py` asserts the source-level mode gate on this category;
    `workspace_ownership.py` refuses imports and ownership flips on it. A type in one list and
    not the other is a gap in whichever check was not updated — and the gap would be silent,
    because each suite passes against its own list.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    import test_networking_modes as source_suite
    from workspace_ownership import NETWORK_OWNING_TYPES as guard_types

    assert frozenset(source_suite.NETWORK_OWNING_TYPES) == guard_types, (
        f"the two network-owning type lists have diverged.\n"
        f"Only in the source suite: "
        f"{sorted(frozenset(source_suite.NETWORK_OWNING_TYPES) - guard_types)}\n"
        f"Only in the plan guard: "
        f"{sorted(guard_types - frozenset(source_suite.NETWORK_OWNING_TYPES))}\n\n"
        f"A type in one and not the other is unprotected in whichever place was not "
        f"updated — the source gate or the import/ownership-flip refusal."
    )


def test_every_allowed_type_has_an_identity_rule() -> None:
    """No allowed type may fall through to the "no identity rule" denial.

    That branch exists to fail closed when someone adds a type to ALLOWED_RESOURCE_TYPES
    without saying how ownership is decided for it. But if a type this module ACTUALLY
    declares lands there, the guard denies every legitimate plan containing it — the
    unusable-guard failure direction the `84e3f7ee` review found, where a guard that cannot
    approve a correct plan gets removed rather than fixed.

    So the branch stays, and this test asserts nothing currently reaches it.
    """
    from workspace_ownership import (
        ADDRESS_IDENTIFIED_TYPES,
        ALLOWED_RESOURCE_TYPES,
        IDENTITY_FIELDS,
        TAG_IDENTIFIED_TYPES,
    )

    covered = (
        frozenset(IDENTITY_FIELDS) | TAG_IDENTIFIED_TYPES | ADDRESS_IDENTIFIED_TYPES
    )
    uncovered = ALLOWED_RESOURCE_TYPES - covered
    assert not uncovered, (
        f"these types are allowed but have no identity rule: {sorted(uncovered)}.\n\n"
        f"Every one would be DENIED at validation time with 'has no identity rule', so a "
        f"legitimate plan containing it never applies. Add each to IDENTITY_FIELDS (it has a "
        f"name), TAG_IDENTIFIED_TYPES (it carries the Workspace tag) or "
        f"ADDRESS_IDENTIFIED_TYPES (it has no plan-time identity at all) in "
        f"workspace_ownership.py, with the reasoning."
    )


def test_every_type_the_module_declares_is_allowed_and_identifiable() -> None:
    """The guard must accept the module it guards.

    Read from the module's own `.tf` source rather than from a list, so adding a resource to
    the Terraform without teaching the guard about it fails here — at the moment the resource
    is added — rather than in a lane against a real account.
    """
    from workspace_ownership import (
        ADDRESS_IDENTIFIED_TYPES,
        ALLOWED_RESOURCE_TYPES,
        IDENTITY_FIELDS,
        TAG_IDENTIFIED_TYPES,
        declared_addresses,
    )

    declared_types = {resource_type for resource_type, _ in declared_addresses()}
    assert declared_types, (
        "declared_addresses() returned nothing; the parser has stopped matching."
    )

    not_allowed = declared_types - ALLOWED_RESOURCE_TYPES
    assert not not_allowed, (
        f"the module declares {sorted(not_allowed)}, which the plan guard's allowlist "
        f"refuses. Either the resource does not belong in a workspace's state, or the policy "
        f"in workspace_ownership.py needs it added with the reasoning — but the two must not "
        f"disagree, because the disagreement surfaces as a denied apply."
    )

    covered = (
        frozenset(IDENTITY_FIELDS) | TAG_IDENTIFIED_TYPES | ADDRESS_IDENTIFIED_TYPES
    )
    unidentifiable = declared_types - covered
    assert not unidentifiable, (
        f"the module declares {sorted(unidentifiable)} with no identity rule in "
        f"workspace_ownership.py, so a plan touching one is denied as unattributable."
    )


def test_a_base_region_estimate_claims_no_multiplier_it_did_not_apply(
    tmp_path,
) -> None:
    """In us-east-1 nothing is scaled, so the caveat must be ABSENT rather than boilerplate.

    The complement of the control above, and the reason the basis field is conditional. A caveat
    attached to every estimate regardless of whether it applies is noise a reviewer learns to skip,
    which costs exactly the case where it mattered. Asserting the absence also proves the field is
    driven by the multiplier rather than pasted in unconditionally.
    """
    plan = _plan(_cluster(["create"]))
    estimate_path = tmp_path / "base.json"
    result = _run(_write(tmp_path, plan), "--estimate", str(estimate_path))
    assert result.returncode == 0, result.stdout + result.stderr

    estimate = json.loads(estimate_path.read_text())
    assert estimate["region_price_multiplier"] == 1.0, (
        f"{REGION} is the rate table's base region and must not be scaled; got "
        f"{estimate['region_price_multiplier']}."
    )
    assert "region_multiplier_basis" not in estimate, (
        "an unscaled estimate must not carry a multiplier caveat. Emitting it unconditionally "
        "makes the field uninformative in the case it exists for."
    )
    assert "region_price_multiplier" not in estimate["upper_bound_scope"], (
        f"the base-region scope statement must not discuss a scaling factor it did not "
        f"apply.\n{estimate['upper_bound_scope']}"
    )
    assert estimate["support_policy_reviewed_on"], (
        "every estimate must date the support policy it priced against; the tier decides the "
        "control-plane rate and a stale calendar is what made it wrong."
    )


@pytest.mark.parametrize(
    ("field", "original", "changed"),
    [
        (
            "configuration",
            {
                "root_module": {
                    "outputs": {"policy": {"expression": {"constant_value": "Allow"}}}
                }
            },
            {
                "root_module": {
                    "outputs": {"policy": {"expression": {"constant_value": "Deny"}}}
                }
            },
        ),
        (
            "planned_values",
            {"root_module": {"resources": [{"values": {"name": "original"}}]}},
            {"root_module": {"resources": [{"values": {"name": "substituted"}}]}},
        ),
        ("output_changes", {"result": {"after": "one"}}, {"result": {"after": "two"}}),
        ("checks", [{"status": "pass"}], [{"status": "fail"}]),
        ("timestamp", "2026-09-20T00:00:00Z", "2026-09-20T00:00:01Z"),
        ("complete", True, 1),
        ("applyable", False, 0),
    ],
)
def test_whole_rendering_mismatch_cannot_emit_authorization(
    tmp_path, field, original, changed
):
    plan = _plan(_cluster(["delete"]))
    if field == "configuration":
        import copy

        original = {
            "root_module": {**plan[field]["root_module"], **original["root_module"]}
        }
        changed = {
            "root_module": {
                **copy.deepcopy(plan[field]["root_module"]),
                **changed["root_module"],
            }
        }
    plan[field] = original
    reviewed = _write(tmp_path, plan)
    artifact = _stub_artifact(tmp_path, reviewed)
    plan[field] = changed
    reviewed = _write(tmp_path, plan)
    authorization = tmp_path / "emitted.json"
    result = _run(
        reviewed,
        "--emit-authorization",
        str(authorization),
        *_artifact_flags(tmp_path, artifact),
    )
    _assert_denied(result, because="is NOT a rendering of")
    assert field in result.stdout + result.stderr
    assert not authorization.exists()


def test_missing_field_does_not_equal_explicit_null(tmp_path):
    plan = _plan(_cluster(["delete"]))
    reviewed = _write(tmp_path, plan)
    artifact = _stub_artifact(tmp_path, reviewed)
    plan["checks"] = None
    reviewed = _write(tmp_path, plan)
    result = _run(
        reviewed,
        "--emit-authorization",
        str(tmp_path / "emitted.json"),
        *_artifact_flags(tmp_path, artifact),
    )
    _assert_denied(result, because="is NOT a rendering of")


@pytest.mark.parametrize(
    "prefix", ["aws_eks_cluster.", "aws_iam_role.", "aws_kms_key."]
)
def test_non_network_imports_cannot_adopt_existing_resources(tmp_path, prefix):
    plan = _genuine_plan(creating=True)
    change = next(
        c for c in plan["resource_changes"] if c["address"].startswith(prefix)
    )
    change["change"]["importing"] = {"id": "pre-existing-resource"}
    _assert_denied(_run(_write(tmp_path, plan)), because="is being IMPORTED")
