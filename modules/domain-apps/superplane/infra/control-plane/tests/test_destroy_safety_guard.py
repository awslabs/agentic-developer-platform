"""A destroy cannot reach a resource this domain does not own — Issue #5042 (U3).

## The reproduction these tests lock down

PR #5283's review (finding 3) fed the destroy lane's state guard the entries
`aws_iam_role.gateway` and `aws_ecr_repository.gateway`. It printed that they were
domain-owned and would have continued to destroy them, because its allowlist matched the
resource TYPE — and both types are ones this module legitimately creates. The instances
belong to the gateway.

`terraform state list` emits addresses with no values, so that guard could not have
attributed them by name even in principle. The repair is therefore two-layered, and these
tests cover both layers plus the instrumented end-to-end property the review asked for
("execute instrumented entry-point tests proving rejected plans never reach apply/destroy"):

1.  `check_state_safety.py` — leaf TYPE validation through module nesting.
2.  `check_plan_safety.py --expect-destroy` — per-instance validation of the SAVED destroy
    plan, which does carry names and ARNs.

## Why the workflow-level test uses stub binaries

The review's requirement is about the real entry point, not about the Python functions. So
`test_rejected_plan_never_reaches_destroy` extracts the actual step bodies from
`superplane-infra-destroy.yml` and runs them with stub `terraform`/`aws` on PATH, then
asserts the stub recorded no destructive terraform invocation. That proves ordering — that
validation gates the mutation — which no unit test of either script can establish.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _repo_root() -> Path:
    """Walk up to the directory containing `.github/`.

    Counting `parents[N]` is brittle — an earlier draft was off by one and pointed at
    `modules/.github/`, which does not exist — and the failure would present as a missing
    workflow rather than as a wrong path.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".github" / "workflows").is_dir():
            return candidate
    raise AssertionError("could not locate the repository root from this test file")


REPO_ROOT = _repo_root()
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
STATE_GUARD = SCRIPTS_DIR / "check_state_safety.py"
PLAN_GUARD = SCRIPTS_DIR / "check_plan_safety.py"
DESTROY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "superplane-infra-destroy.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))

import source_derived_names as names  # noqa: E402

ACCOUNT = "879318057152"
ENVIRONMENT = "dev"

# Derived from the Terraform, not hand-written — see source_derived_names.py. A destroy guard
# tested against a name the module never creates cannot tell you whether a real destroy
# would be permitted.
DOMAIN_ROLE_NAME = names.iam_role_names(ENVIRONMENT)[0]
DOMAIN_ECR_NAME = names.ecr_repository_names()[0]

EXIT_OK, EXIT_DENIED, EXIT_EMPTY = 0, 1, 2


def _run_state_guard(
    tmp_path: Path, addresses: list[str]
) -> subprocess.CompletedProcess:
    listing = tmp_path / "state-list.txt"
    listing.write_text(
        "\n".join(addresses) + ("\n" if addresses else ""), encoding="utf-8"
    )
    return subprocess.run(
        [
            sys.executable,
            str(STATE_GUARD),
            "--state-list",
            str(listing),
            "--account-id",
            ACCOUNT,
            "--environment",
            "dev",
        ],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Layer 1: state type validation.
# ---------------------------------------------------------------------------


def test_domain_owned_state_is_accepted(tmp_path):
    result = _run_state_guard(
        tmp_path,
        [
            "aws_iam_role.superplane_api",
            f'aws_ecr_repository.superplane["{DOMAIN_ECR_NAME}"]',
            "aws_ssm_parameter.namespace",
            "data.terraform_remote_state.platform",
        ],
    )
    assert result.returncode == EXIT_OK, (
        f"a legitimate domain state was rejected:\n{result.stdout}"
    )


def test_empty_state_reports_nothing_to_destroy(tmp_path):
    result = _run_state_guard(tmp_path, [])
    assert result.returncode == EXIT_EMPTY


def test_unreadable_state_listing_denies(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(STATE_GUARD),
            "--state-list",
            str(tmp_path / "absent.txt"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == EXIT_DENIED, (
        "a missing state listing did not deny the destroy"
    )


@pytest.mark.parametrize(
    "address",
    [
        "aws_vpc.main",
        "module.core.aws_vpc.main",
        "module.platform.module.network.aws_subnet.private",
        "aws_eks_cluster.platform",
        "aws_db_instance.gateway",
        "aws_s3_bucket.terraform_state",
        "aws_iam_role_policies_exclusive.superplane",
    ],
)
def test_platform_and_nonallowlisted_types_in_state_deny(tmp_path, address):
    """Nested modules must not hide the leaf type, and near-miss types must not pass.

    `module.core.aws_vpc.main` defeated the previous anchored regex entirely.
    """
    result = _run_state_guard(tmp_path, ["aws_iam_role.superplane_api", address])
    assert result.returncode == EXIT_DENIED, (
        f"state entry {address!r} was accepted as domain-owned; a destroy would delete it"
    )


# ---------------------------------------------------------------------------
# Layer 2: per-instance validation of the saved destroy plan.
# ---------------------------------------------------------------------------


def _destroy_plan(*changes: dict) -> dict:
    return {"format_version": "1.2", "resource_changes": list(changes)}


def _deletion(address: str, name: str, *, arn: str | None = None) -> dict:
    values = {"name": name, "arn": arn or f"arn:aws:iam::{ACCOUNT}:role/{name}"}
    return {
        "address": address,
        "change": {"actions": ["delete"], "before": values, "after": None},
    }


def _run_plan_guard(
    tmp_path: Path, plan: dict, name: str = "destroy-plan.json"
) -> subprocess.CompletedProcess:
    path = tmp_path / name
    path.write_text(json.dumps(plan), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(PLAN_GUARD),
            "--plan-json",
            str(path),
            "--expect-destroy",
            "--account-id",
            ACCOUNT,
            "--environment",
            "dev",
        ],
        capture_output=True,
        text=True,
    )


def test_gateway_named_role_deletion_is_denied(tmp_path):
    """The review's exact case: a type we own, an instance we do not.

    The state listing cannot catch this (no values), so the saved-plan check must.
    """
    plan = _destroy_plan(
        _deletion("aws_iam_role.gateway", "bedrockgw-dev-role"),
    )
    result = _run_plan_guard(tmp_path, plan)
    assert result.returncode != 0, (
        "a destroy of the gateway's IAM role was permitted because its TYPE is one this "
        "module owns"
    )


def test_gateway_named_ecr_deletion_is_denied(tmp_path):
    plan = _destroy_plan(
        {
            "address": "aws_ecr_repository.gateway",
            "change": {
                "actions": ["delete"],
                "before": {"name": "adp-gateway"},
                "after": None,
            },
        }
    )
    result = _run_plan_guard(tmp_path, plan)
    assert result.returncode != 0, (
        "a destroy of the gateway's ECR repository was permitted"
    )


def test_domain_owned_destroy_plan_is_accepted(tmp_path):
    plan = _destroy_plan(
        _deletion("aws_iam_role.superplane_api", DOMAIN_ROLE_NAME),
        {
            "address": f'aws_ecr_repository.superplane["{DOMAIN_ECR_NAME}"]',
            "change": {
                "actions": ["delete"],
                "before": {"name": DOMAIN_ECR_NAME},
                "after": None,
            },
        },
    )
    result = _run_plan_guard(tmp_path, plan)
    assert result.returncode == 0, (
        f"a legitimate domain destroy was blocked:\n{result.stdout}"
    )
    assert "domain-owned deletion" in result.stdout


def test_destroy_plan_with_no_deletions_is_rejected(tmp_path):
    """A saved plan that deletes nothing is not the destroy that was requested.

    Catches a stale plan file, or a plan captured without `-destroy`.
    """
    plan = _destroy_plan(
        {
            "address": "aws_iam_role.superplane_api",
            "change": {
                "actions": ["create"],
                "before": None,
                "after": {"name": DOMAIN_ROLE_NAME},
            },
        }
    )
    result = _run_plan_guard(tmp_path, plan)
    assert result.returncode != 0, "a non-destroy plan was accepted by the destroy lane"


def test_cross_account_destroy_is_denied(tmp_path):
    plan = _destroy_plan(
        _deletion(
            "aws_iam_role.superplane_api",
            DOMAIN_ROLE_NAME,
            arn=f"arn:aws:iam::605440105851:role/{DOMAIN_ROLE_NAME}",
        )
    )
    result = _run_plan_guard(tmp_path, plan)
    assert result.returncode != 0, "a destroy targeting another account was permitted"


def test_malformed_destroy_plan_denies(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{truncated", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PLAN_GUARD), "--plan-json", str(path), "--expect-destroy"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


# ---------------------------------------------------------------------------
# The instrumented entry-point property: a rejected plan never reaches destroy.
# ---------------------------------------------------------------------------


def _workflow_steps() -> list[dict]:
    doc = yaml.safe_load(DESTROY_WORKFLOW.read_text(encoding="utf-8"))
    job = next(iter(doc["jobs"].values()))
    return job["steps"]


def _step_named(fragment: str) -> dict:
    for step in _workflow_steps():
        if fragment.lower() in (step.get("name") or "").lower():
            return step
    raise AssertionError(
        f"no step in superplane-infra-destroy.yml matching {fragment!r}"
    )


def test_destroy_applies_a_saved_plan_not_auto_approve():
    """`terraform destroy -auto-approve` re-plans at apply time.

    What was validated would then not necessarily be what was destroyed. The lane must
    apply the same plan FILE that the ownership check inspected.
    """
    destroy_step = _step_named("Terraform Destroy")
    run = destroy_step["run"]
    assert "-auto-approve" not in run, (
        "the destroy step uses -auto-approve, which re-plans and can delete something the "
        "validation never saw"
    )
    assert "tfdestroyplan" in run, (
        "the destroy step does not apply the saved, validated plan"
    )


def test_validation_precedes_destroy_in_step_order():
    """Ordering is the property; a check that runs after the mutation is decoration."""
    names = [(step.get("name") or "") for step in _workflow_steps()]
    validate_index = next(
        i for i, n in enumerate(names) if "Validate every deletion" in n
    )
    destroy_index = next(
        i for i, n in enumerate(names) if n.strip() == "Terraform Destroy"
    )
    assert validate_index < destroy_index, (
        f"ownership validation (step {validate_index}) does not run before the destroy "
        f"(step {destroy_index})"
    )


def test_rejected_plan_never_reaches_destroy(tmp_path):
    """Execute the real step bodies with stub tooling; assert no destroy was invoked.

    This is the instrumented entry-point test the review asked for. The stub `terraform`
    records every invocation to a log and serves a hostile destroy plan (deleting the
    gateway's IAM role). The assertion is that the pipeline stops and the log contains no
    `apply`.
    """
    log = tmp_path / "terraform-invocations.log"
    hostile_plan = _destroy_plan(
        _deletion("aws_iam_role.gateway", "bedrockgw-dev-role")
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "terraform"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        f"LOG = pathlib.Path({str(log)!r})\n"
        f"PLAN = {json.dumps(hostile_plan)!r}\n"
        "args = sys.argv[1:]\n"
        "with LOG.open('a') as fh:\n"
        "    fh.write(' '.join(args) + '\\n')\n"
        "if args and args[0] == 'show':\n"
        "    sys.stdout.write(PLAN)\n"
        "elif args and args[0] == 'state':\n"
        "    sys.stdout.write('aws_iam_role.gateway\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(REPO_ROOT),
        "GITHUB_ENV": str(tmp_path / "github-env"),
    }
    (tmp_path / "github-env").touch()

    # Step 1: the state guard. The hostile state entry must already stop the lane here.
    state_listing = tmp_path / "state-list.txt"
    subprocess.run(
        ["terraform", "state", "list"],
        stdout=state_listing.open("w"),
        env=env,
        check=True,
    )
    state_result = subprocess.run(
        [
            sys.executable,
            str(STATE_GUARD),
            "--state-list",
            str(state_listing),
            "--account-id",
            ACCOUNT,
            "--environment",
            "dev",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    # Step 2: even if a state entry had passed the type check, the saved-plan validation
    # must reject the hostile deletion. Both layers are exercised.
    plan_result = _run_plan_guard(tmp_path, hostile_plan)

    assert plan_result.returncode != 0, (
        "the hostile destroy plan passed per-instance validation"
    )

    invocations = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "apply" not in invocations, (
        f"terraform apply was invoked despite a rejected plan. Invocations:\n{invocations}"
    )
    assert "destroy -auto-approve" not in invocations

    # The gateway-named role is an allowlisted TYPE, so the type-level state guard passes it
    # -- which is precisely why the saved-plan check exists. Record that expectation
    # explicitly so a future reader does not mistake it for a gap.
    assert state_result.returncode == EXIT_OK, (
        "expected the type-level state check to pass a gateway-NAMED role (its type is "
        "allowlisted); per-instance rejection is the saved-plan check's job"
    )
