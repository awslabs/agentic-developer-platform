"""The refresh-only migration guard must refuse, not merely report — Issue #5831.

## Why this suite exists

The runbook's step-4 inspection was originally a snippet that printed three counts
and exited 0 whatever they were, while the surrounding prose claimed it "refused"
an unsafe plan. That is worse than having no check: it reads as a gate, so an
operator trusts it, and it cannot stop anything.

`platform/scripts/refresh_only_migration_guard.py` replaced it. The point of this
suite is therefore **not** that the guard runs — it is that the guard *fails
non-zero* on each condition that must block the state-writing apply. A guard is
only worth its name if its refusal legs are tested, so every prohibited condition
below asserts `exit 1`, and the one permitted condition asserts `exit 0`.

## Why the fixtures are synthetic

A real `terraform plan -refresh-only` **cannot be produced offline**: `-refresh-only`
exists to reconcile state against the provider, so it makes live API calls and
fails without credentials (verified — it errors `AuthFailure` on the launch
template even with `-refresh=false`-style credential stubs in place). So this suite
builds saved-plan files directly: a plan file is a ZIP carrying `tfstate` and
`tfstate-prev` members, which is the shape the guard reads and the shape root
verified on the real backend.

That is a real limit, stated rather than papered over: these tests establish the
guard's logic, not that any particular real plan is safe. The real plan's review is
root's, using this guard.

## Scope

Pure file construction and one subprocess per leg. No AWS call, no Terraform
invocation, no state write, no network. This suite needs no `terraform` binary.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD = REPO_ROOT / "platform" / "scripts" / "refresh_only_migration_guard.py"

# Stands in for the real platform state's shape: an add-on whose record is newer
# than provider 5.x understands, and a launch template whose record is older than
# the provider's schema. Synthetic throughout — account 000000000000, placeholder
# ARNs — so no real identifier enters the repository.
STALE = "aws_launch_template.gvisor_nodes"
NEWER = "aws_eks_addon.coredns"


def _state(resources: list[dict], serial: int = 84) -> dict:
    return {
        "version": 4,
        "terraform_version": "1.14.6",
        "serial": serial,
        "lineage": "00000000-0000-0000-0000-000000000000",
        "outputs": {},
        "check_results": None,
        "resources": resources,
    }


def _resource(
    address: str,
    *,
    schema_version: int,
    resource_id: str,
    arn: str | None = None,
    name: str | None = None,
    module: str | None = None,
) -> dict:
    kind, local = address.split(".", 1)
    attributes: dict[str, object] = {"id": resource_id}
    if arn is not None:
        attributes["arn"] = arn
    if name is not None:
        attributes["name"] = name
    record = {
        "mode": "managed",
        "type": kind,
        "name": local,
        "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
        "instances": [{"schema_version": schema_version, "attributes": attributes}],
    }
    if module:
        record["module"] = module
    return record


def _preserved_pair() -> tuple[dict, dict]:
    """Prior and resulting state for a correct migration.

    The launch template's `schema_version` rises 0 -> 1: that IS the migration's
    purpose, so the guard must permit it while still refusing identity changes.
    Identity fields are deliberately identical on both sides.
    """
    def records(stale_schema: int) -> list[dict]:
        return [
            _resource(
                NEWER,
                schema_version=0,
                resource_id="adp-dev:coredns",
                arn="arn:aws:eks:us-east-1:000000000000:addon/adp-dev/coredns",
                name="coredns",
                module="module.eks",
            ),
            _resource(
                STALE,
                schema_version=stale_schema,
                resource_id="lt-0123456789abcdef0",
                arn="arn:aws:ec2:us-east-1:000000000000:launch-template/lt-0123456789abcdef0",
                name="adp-dev-gvisor",
            ),
        ]

    return _state(records(0)), _state(records(1), serial=85)


def _write_plan(path: Path, prior: dict, result: dict, *, omit: str = "") -> Path:
    """Build a saved-plan file: a ZIP with `tfstate` and `tfstate-prev` members."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("tfplan", "synthetic plan body")
        if omit != "tfstate-prev":
            archive.writestr("tfstate-prev", json.dumps(prior))
        if omit != "tfstate":
            archive.writestr("tfstate", json.dumps(result))
    return path


def _write_json(path: Path, document: dict) -> Path:
    path.write_text(json.dumps(document))
    return path


def _no_op_plan_json(addresses: tuple[str, ...] = (NEWER, STALE)) -> dict:
    return {
        "format_version": "1.2",
        "resource_changes": [
            {"address": address, "change": {"actions": ["no-op"]}}
            for address in addresses
        ],
        "resource_drift": [],
    }


def _run_guard(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def preserved_case(tmp_path: Path) -> tuple[Path, Path]:
    """The one case that must PASS: no changes, nothing lost, identities intact."""
    prior, result = _preserved_pair()
    plan = _write_plan(tmp_path / "migrate.tfplan", prior, result)
    plan_json = _write_json(tmp_path / "migrate.json", _no_op_plan_json())
    return plan, plan_json


def test_guard_exists_and_is_executable() -> None:
    assert GUARD.is_file(), f"{GUARD} must exist."


# ---------------------------------------------------------------------------
# The permitted case. If this ever fails, the guard refuses correct migrations —
# which teaches operators to bypass it, so it matters as much as the refusals.
# ---------------------------------------------------------------------------


def test_preserved_no_change_plan_passes(preserved_case: tuple[Path, Path]) -> None:
    plan, plan_json = preserved_case
    result = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json), "--show-exit-code", "0"
    )
    assert result.returncode == 0, (
        "a refresh-only plan that proposes nothing and preserves every address must "
        f"pass; the guard refused with: {result.stdout} {result.stderr}"
    )
    assert "PASS" in result.stdout
    assert "managed resources preserved: 2 -> 2" in result.stdout


def test_schema_version_upgrade_alone_is_permitted(
    preserved_case: tuple[Path, Path],
) -> None:
    """The migration's entire purpose is raising schema_version, so it cannot refuse it.

    Asserted separately from the pass case because it is the one difference the
    fixture's two state members carry — if the guard ever starts comparing whole
    objects, this is the test that catches it.
    """
    plan, plan_json = preserved_case
    with zipfile.ZipFile(plan) as archive:
        prior = json.loads(archive.read("tfstate-prev"))
        result_state = json.loads(archive.read("tfstate"))

    def schema_for(state: dict) -> int:
        return next(
            record["instances"][0]["schema_version"]
            for record in state["resources"]
            if record["type"] == STALE.split(".")[0]
        )

    assert schema_for(prior) == 0 and schema_for(result_state) == 1, (
        "the fixture must differ in schema_version across the migration, or this "
        "assertion proves nothing."
    )
    assert (
        _run_guard(
            "--plan-file", str(plan), "--plan-json", str(plan_json),
            "--show-exit-code", "0",
        ).returncode
        == 0
    )


# ---------------------------------------------------------------------------
# The refusals. Each must exit non-zero: this is what the old snippet could not do.
# ---------------------------------------------------------------------------


def test_ordinary_full_plan_is_refused(tmp_path: Path) -> None:
    """An ordinary full plan proposes destroying the undeclared add-on — refuse it.

    This is the mistake with the worst consequence: on the real cluster that
    resource is CoreDNS. The runbook warns against it in prose; this makes the
    warning enforceable.
    """
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "full.tfplan", prior, result_state)
    plan_json = _write_json(
        tmp_path / "full.json",
        {
            "format_version": "1.2",
            "resource_changes": [
                {"address": NEWER, "change": {"actions": ["delete"]}},
                {"address": STALE, "change": {"actions": ["no-op"]}},
            ],
            "resource_drift": [],
        },
    )
    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json), "--show-exit-code", "0"
    )
    assert outcome.returncode == 1, "a plan proposing a delete must be refused."
    assert "FAIL" in outcome.stdout
    assert NEWER in outcome.stdout


def test_refreshed_deletion_is_refused(tmp_path: Path) -> None:
    """A drift entry proposing delete means applying would drop state. Refuse."""
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "drift.tfplan", prior, result_state)
    plan_json = _write_json(
        tmp_path / "drift.json",
        {
            "format_version": "1.2",
            "resource_changes": [
                {"address": address, "change": {"actions": ["no-op"]}}
                for address in (NEWER, STALE)
            ],
            "resource_drift": [
                {"address": NEWER, "change": {"actions": ["delete"]}},
            ],
        },
    )
    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json), "--show-exit-code", "0"
    )
    assert outcome.returncode == 1, "a drift deletion must be refused."
    assert "drift" in outcome.stdout.lower()


def test_resource_loss_is_refused(tmp_path: Path) -> None:
    """A managed address present before and absent after is the core loss case."""
    prior, result_state = _preserved_pair()
    result_state["resources"] = [
        record
        for record in result_state["resources"]
        if record["type"] != NEWER.split(".")[0]
    ]
    plan = _write_plan(tmp_path / "loss.tfplan", prior, result_state)
    plan_json = _write_json(tmp_path / "loss.json", _no_op_plan_json())

    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json), "--show-exit-code", "0"
    )
    assert outcome.returncode == 1, (
        "a managed resource dropped by the migration must be refused — this is the "
        "loss the guard exists to prevent."
    )
    assert "absent" in outcome.stdout


def test_identity_change_is_refused(tmp_path: Path) -> None:
    """A changed id means state would point at a different object. Refuse."""
    prior, result_state = _preserved_pair()
    for record in result_state["resources"]:
        if record["type"] == STALE.split(".")[0]:
            record["instances"][0]["attributes"]["id"] = "lt-9999999999999999"
    plan = _write_plan(tmp_path / "identity.tfplan", prior, result_state)
    plan_json = _write_json(tmp_path / "identity.json", _no_op_plan_json())

    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json), "--show-exit-code", "0"
    )
    assert outcome.returncode == 1, "a changed identity field must be refused."
    assert f"{STALE}.id" in outcome.stdout
    assert "lt-9999999999999999" not in outcome.stdout, (
        "the guard must name the field that differs, never the value: state carries "
        "attribute values and these logs are readable."
    )


def test_failed_export_is_refused(preserved_case: tuple[Path, Path]) -> None:
    """A non-zero `terraform show` exit is the #5831 failure itself — never a pass."""
    plan, plan_json = preserved_case
    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json), "--show-exit-code", "1"
    )
    assert outcome.returncode == 1
    assert "exited 1" in outcome.stdout


def test_truncated_plan_json_is_refused(tmp_path: Path) -> None:
    """A truncated export must not read as "zero changes, proceed"."""
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "trunc.tfplan", prior, result_state)
    truncated = tmp_path / "trunc.json"
    truncated.write_text('{"resource_changes": [')

    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(truncated), "--show-exit-code", "0"
    )
    assert outcome.returncode == 1


def test_plan_json_without_resource_changes_is_refused(tmp_path: Path) -> None:
    """Absent is not empty: no `resource_changes` key means nothing was shown."""
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "nokey.tfplan", prior, result_state)
    plan_json = _write_json(tmp_path / "nokey.json", {"format_version": "1.2"})

    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json), "--show-exit-code", "0"
    )
    assert outcome.returncode == 1
    assert "resource_changes" in outcome.stdout


def test_plan_without_state_members_is_refused(tmp_path: Path) -> None:
    """If the plan carries no state, preservation cannot be established. Refuse.

    Directly guards the decorative-gate failure mode: treating a missing member as
    "nothing lost" would let every preservation leg vacuously pass.
    """
    prior, result_state = _preserved_pair()
    plan = _write_plan(
        tmp_path / "nostate.tfplan", prior, result_state, omit="tfstate-prev"
    )
    plan_json = _write_json(tmp_path / "nostate.json", _no_op_plan_json())

    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json), "--show-exit-code", "0"
    )
    assert outcome.returncode == 1
    assert "tfstate-prev" in outcome.stdout


def test_plan_json_passed_as_plan_file_is_refused(tmp_path: Path) -> None:
    """A JSON export is not a saved plan. Refuse rather than misread it."""
    plan_json = _write_json(tmp_path / "only.json", _no_op_plan_json())
    outcome = _run_guard(
        "--plan-file", str(plan_json), "--plan-json", str(plan_json),
        "--show-exit-code", "0",
    )
    assert outcome.returncode == 1
    assert "not a readable plan file" in outcome.stdout


# ---------------------------------------------------------------------------
# Optional legs: the preserved snapshot and the expected count.
# ---------------------------------------------------------------------------


def test_snapshot_missing_address_is_refused(tmp_path: Path) -> None:
    """A resource dropped BEFORE the plan was generated is absent from both members.

    Without this comparison it would pass every other leg, because the guard would
    never have seen it. That is why the snapshot from step 1 is worth taking.
    """
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "snap.tfplan", prior, result_state)
    plan_json = _write_json(tmp_path / "snap.json", _no_op_plan_json())

    snapshot = _state(
        prior["resources"]
        + [_resource("aws_s3_bucket.extra", schema_version=0, resource_id="adp-extra")]
    )
    snapshot_path = _write_json(tmp_path / "pre.tfstate", snapshot)

    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json),
        "--show-exit-code", "0", "--preserved-snapshot", str(snapshot_path),
    )
    assert outcome.returncode == 1
    assert "aws_s3_bucket.extra" in outcome.stdout


def test_matching_snapshot_and_count_pass(tmp_path: Path) -> None:
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "ok.tfplan", prior, result_state)
    plan_json = _write_json(tmp_path / "ok.json", _no_op_plan_json())
    snapshot_path = _write_json(tmp_path / "pre.tfstate", prior)

    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json),
        "--show-exit-code", "0", "--preserved-snapshot", str(snapshot_path),
        "--expect-resources", "2",
    )
    assert outcome.returncode == 0, f"refused a matching case: {outcome.stdout}"


def test_wrong_expected_count_is_refused(tmp_path: Path) -> None:
    """The count root established on the real state (136) must be assertable.

    If the plan under review does not carry it, either it is the wrong plan or state
    moved since — both need re-review rather than an apply.
    """
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "count.tfplan", prior, result_state)
    plan_json = _write_json(tmp_path / "count.json", _no_op_plan_json())

    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json),
        "--show-exit-code", "0", "--expect-resources", "136",
    )
    assert outcome.returncode == 1
    assert "136" in outcome.stdout


def test_module_and_indexed_addresses_are_compared_by_full_address(
    tmp_path: Path,
) -> None:
    """Addresses must include module path and index, or distinct resources collide.

    The real platform state is almost entirely module-nested (`module.eks....`), so a
    guard comparing only `type.name` could match two different resources and miss a
    genuine loss.
    """
    indexed = _resource(
        "aws_subnet.private", schema_version=0, resource_id="subnet-a", module="module.vpc"
    )
    indexed["instances"][0]["index_key"] = 0
    second = json.loads(json.dumps(indexed))
    second["instances"][0]["index_key"] = 1
    second["instances"][0]["attributes"]["id"] = "subnet-b"
    indexed["instances"].append(second["instances"][0])

    prior = _state([indexed])
    after = json.loads(json.dumps(prior))
    after["resources"][0]["instances"] = [after["resources"][0]["instances"][0]]

    plan = _write_plan(tmp_path / "idx.tfplan", prior, after)
    plan_json = _write_json(tmp_path / "idx.json", _no_op_plan_json(()))

    outcome = _run_guard(
        "--plan-file", str(plan), "--plan-json", str(plan_json), "--show-exit-code", "0"
    )
    assert outcome.returncode == 1, (
        "dropping one instance of an indexed resource must be refused; if addresses "
        "omitted the index, both instances would collide into one key and the loss "
        "would be invisible."
    )
    assert "module.vpc.aws_subnet.private[1]" in outcome.stdout
