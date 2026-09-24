"""The refresh-only migration guard must refuse, not merely report — Issue #5831.

## Why this suite exists

The runbook's step-4 inspection was originally a snippet that printed three counts
and exited 0 whatever they were, while the surrounding prose claimed it "refused"
an unsafe plan. That is worse than having no check: it reads as a gate, so an
operator trusts it, and it cannot stop anything.

`platform/scripts/refresh_only_migration_guard.py` replaced it. The point of this
suite is therefore **not** that the guard runs — it is that the guard *fails
non-zero* on each condition that must block the state-writing apply, **and passes
the one document shape that must be allowed through.**

## The regression this suite now pins

An earlier version of the guard refused root's real, successful Terraform 1.14.9 /
provider 6.66.0 refresh-only export, reporting that it "has no `resource_changes`
key". Terraform **omits** that key from the JSON of a plan proposing no resource
changes — which is precisely what a correct refresh-only plan is. So the guard was
refusing the artifact it exists to approve, while all of its synthetic tests
passed, because every fixture here had been written with the key present.

`test_real_refresh_only_export_shape_passes` is the fix's regression test: it reads
the sanitized shape fixture recording root's actual top-level key set and asserts a
**pass**. The fixture was corrected, not the real artifact.

## How the Terraform invocation is exercised

The guard now runs `terraform show -json` on the exact `--plan-file` and checks
that exit code itself, instead of accepting a caller-supplied JSON plus an asserted
`--show-exit-code 0`. That closes a real hole — an operator could pass a stale or
unrelated export and a hardcoded success — but it means the export is no longer a
test input, so these tests supply a **stub** binary via `--terraform`:

* it records its own argv, so a test can assert the guard passed the exact plan
  path rather than some other file;
* it emits a chosen document and a chosen exit code, which is how the malformed,
  errored and failed-export legs are driven.

`test_guard_links_export_to_the_exact_plan_file` is the one that makes the linkage
assertable rather than assumed.

## Why the plan fixtures are synthetic

A real `terraform plan -refresh-only` **cannot be produced offline**: `-refresh-only`
exists to reconcile state against the provider, so it makes live API calls and
fails without credentials (verified — it errors `AuthFailure` on the launch
template). So the saved plans here are built directly: a plan file is a ZIP
carrying `tfstate` and `tfstate-prev` members, which is the shape the guard reads
and the shape root verified on the real backend.

That is a real limit, stated rather than papered over: **these tests establish the
guard's logic, not that any particular real plan is safe.** The complementary
check — the guard driven by the real `terraform` binary against a real saved plan —
lives in `test_platform_mixed_state_export.py`, which has a Terraform binary
available. The real plan's review is root's, using this guard.

## Scope

File construction and one subprocess per leg, with a stubbed Terraform. No AWS
call, no real Terraform invocation, no state write, no network. This suite needs no
`terraform` binary.
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
REAL_SHAPE_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "platform-mixed-state-5831"
    / "real-refresh-only-export.json"
)

# Stands in for the real platform state's shape: an add-on whose record is newer
# than provider 5.x understands, and a launch template whose record is older than
# the provider's schema. Synthetic throughout — account 000000000000, placeholder
# ARNs — so no real identifier enters the repository.
STALE = "aws_launch_template.gvisor_nodes"
NEWER = "aws_eks_addon.coredns"


def _state(resources: list[dict], serial: int = 84, lineage: str | None = None) -> dict:
    return {
        "version": 4,
        "terraform_version": "1.14.6",
        "serial": serial,
        "lineage": lineage or "00000000-0000-0000-0000-000000000000",
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
    """A complete export that *does* carry resource_changes, all no-op.

    Terraform emits this shape when it has entries to report. The other permitted
    shape — the key omitted entirely — is the real fixture, asserted separately.
    """
    return {
        "format_version": "1.2",
        "terraform_version": "1.14.9",
        "complete": True,
        "errored": False,
        "resource_changes": [
            {"address": address, "change": {"actions": ["no-op"]}}
            for address in addresses
        ],
        "resource_drift": [],
    }


# ---------------------------------------------------------------------------
# The Terraform stub. The guard exports the plan itself now, so the export is no
# longer a test input: it is produced by this stub, which also records the argv the
# guard used so the plan-file linkage can be asserted.
# ---------------------------------------------------------------------------

_STUB = '''\
import json, os, sys
with open(os.environ["STUB_ARGV_LOG"], "w") as handle:
    json.dump(sys.argv[1:], handle)
body = os.environ.get("STUB_STDOUT", "")
if body:
    sys.stdout.write(body)
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
'''


def _make_stub(tmp_path: Path) -> tuple[Path, Path]:
    """A fake `terraform` that emits a chosen document and records its argv."""
    stub_py = tmp_path / "stub_terraform.py"
    stub_py.write_text(_STUB)
    argv_log = tmp_path / "stub_argv.json"
    shim = tmp_path / "terraform_shim"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub_py}" "$@"\n')
    shim.chmod(0o755)
    return shim, argv_log


def _run_guard(
    *args: str,
    tmp_path: Path,
    stdout: object = None,
    exit_code: int = 0,
    raw_stdout: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the guard with a stubbed `terraform show`.

    `stdout` is the document the stub will emit (JSON-encoded); `raw_stdout` sends
    arbitrary bytes instead, for the truncated-output leg.
    """
    shim, argv_log = _make_stub(tmp_path)
    body = raw_stdout if raw_stdout is not None else json.dumps(stdout or {})
    env = {
        "PATH": "/usr/bin:/bin",
        "STUB_ARGV_LOG": str(argv_log),
        "STUB_STDOUT": body,
        "STUB_EXIT": str(exit_code),
    }
    completed = subprocess.run(
        [sys.executable, str(GUARD), *args, "--terraform", str(shim)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    completed.stub_argv = (  # type: ignore[attr-defined]
        json.loads(argv_log.read_text()) if argv_log.exists() else None
    )
    return completed


@pytest.fixture
def preserved_plan(tmp_path: Path) -> Path:
    """A saved plan whose two state members show a correct, lossless migration."""
    prior, result = _preserved_pair()
    return _write_plan(tmp_path / "migrate.tfplan", prior, result)


def test_guard_exists_and_is_executable() -> None:
    assert GUARD.is_file(), f"{GUARD} must exist."


# ---------------------------------------------------------------------------
# The permitted cases. If either fails, the guard refuses correct migrations —
# which is the defect this round fixed, and which teaches operators to bypass it.
# ---------------------------------------------------------------------------


def test_real_refresh_only_export_shape_passes(
    preserved_plan: Path, tmp_path: Path
) -> None:
    """The guard must PASS root's real export shape, which omits `resource_changes`.

    This is the regression test for the reported defect. The previous guard refused
    this exact document as "has no 'resource_changes' key"; Terraform omits that key
    when the plan proposes nothing, so absence is the *expected* output of a correct
    refresh-only plan, not evidence of truncation.

    Reads the sanitized fixture recording the real top-level key set rather than a
    shape invented here — a fixture written to match the guard would not have caught
    this in the first place.
    """
    document = json.loads(REAL_SHAPE_FIXTURE.read_text())
    assert "resource_changes" not in document, (
        "this fixture's whole purpose is the absent key; if a future edit adds it, "
        "the regression is no longer covered. Correct the guard, not the fixture."
    )

    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=document
    )
    assert outcome.returncode == 0, (
        "the guard refused a legitimate complete export whose optional "
        f"resource_changes field is omitted: {outcome.stdout} {outcome.stderr}"
    )
    assert "PASS" in outcome.stdout


def test_preserved_no_change_plan_passes(preserved_plan: Path, tmp_path: Path) -> None:
    """The other permitted shape: `resource_changes` present, every entry no-op."""
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=_no_op_plan_json()
    )
    assert outcome.returncode == 0, (
        "a refresh-only plan that proposes nothing and preserves every address must "
        f"pass; the guard refused with: {outcome.stdout} {outcome.stderr}"
    )
    assert "managed resources preserved: 2 -> 2" in outcome.stdout


def test_schema_version_upgrade_alone_is_permitted(
    preserved_plan: Path, tmp_path: Path
) -> None:
    """The migration's entire purpose is raising schema_version, so it cannot refuse it.

    Asserted separately from the pass case because it is the one difference the
    fixture's two state members carry — if the guard ever starts comparing whole
    objects, this is the test that catches it.
    """
    with zipfile.ZipFile(preserved_plan) as archive:
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
            "--plan-file", str(preserved_plan), tmp_path=tmp_path,
            stdout=_no_op_plan_json(),
        ).returncode
        == 0
    )


# ---------------------------------------------------------------------------
# The export must describe the plan under review, not some other file.
# ---------------------------------------------------------------------------


def test_guard_links_export_to_the_exact_plan_file(
    preserved_plan: Path, tmp_path: Path
) -> None:
    """The guard must export `--plan-file` itself, not accept a supplied JSON.

    Previously the caller passed a JSON path and separately asserted
    `--show-exit-code 0`, so a stale or unrelated export plus a hardcoded success
    read as a reviewed plan. Asserting the argv is what makes the binding checkable
    instead of assumed.
    """
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=_no_op_plan_json()
    )
    assert outcome.returncode == 0
    assert outcome.stub_argv is not None, "the guard never invoked terraform."
    assert outcome.stub_argv[:2] == ["show", "-json"], outcome.stub_argv
    assert outcome.stub_argv[2] == str(preserved_plan.resolve()), (
        "the guard must show the exact plan file it was given; it passed "
        f"{outcome.stub_argv[2]!r}"
    )


def test_guard_no_longer_accepts_an_asserted_show_exit_code(
    preserved_plan: Path, tmp_path: Path
) -> None:
    """The flags that let a caller assert success must be gone, not merely unused."""
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), "--show-exit-code", "0",
        tmp_path=tmp_path, stdout=_no_op_plan_json(),
    )
    assert outcome.returncode != 0, (
        "--show-exit-code must be rejected: accepting it would let the runbook go on "
        "hardcoding success for an export the guard did not perform."
    )


def test_failed_export_is_refused(preserved_plan: Path, tmp_path: Path) -> None:
    """A non-zero `terraform show` exit is the #5831 failure itself — never a pass."""
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout={}, exit_code=1
    )
    assert outcome.returncode == 1
    assert "exited 1" in outcome.stdout


def test_missing_terraform_binary_is_refused(preserved_plan: Path) -> None:
    """No export means no review. Refuse rather than proceeding on the ZIP alone."""
    outcome = subprocess.run(
        [
            sys.executable, str(GUARD), "--plan-file", str(preserved_plan),
            "--terraform", "/nonexistent/terraform",
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert outcome.returncode == 1
    assert "not found" in outcome.stdout


def test_nonexistent_plan_file_is_refused(tmp_path: Path) -> None:
    outcome = _run_guard(
        "--plan-file", str(tmp_path / "absent.tfplan"), tmp_path=tmp_path,
        stdout=_no_op_plan_json(),
    )
    assert outcome.returncode == 1
    assert "does not exist" in outcome.stdout


# ---------------------------------------------------------------------------
# Malformed / wrong-kind documents. These are what "absent is not empty" was
# reaching for, and what it must refuse now that absence itself is permitted.
# ---------------------------------------------------------------------------


def test_truncated_export_is_refused(preserved_plan: Path, tmp_path: Path) -> None:
    """A truncated export must not read as "zero changes, proceed"."""
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path,
        raw_stdout='{"resource_changes": [',
    )
    assert outcome.returncode == 1
    assert "does not parse" in outcome.stdout


def test_document_without_format_version_is_refused(
    preserved_plan: Path, tmp_path: Path
) -> None:
    """A document that is not a plan export cannot stand in for one.

    With `resource_changes` no longer required, `format_version` is what
    distinguishes a plan export from an arbitrary JSON object — otherwise `{}` would
    read as a complete plan proposing nothing.
    """
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout={"resources": []}
    )
    assert outcome.returncode == 1
    assert "format_version" in outcome.stdout


def test_empty_document_is_refused(preserved_plan: Path, tmp_path: Path) -> None:
    """The narrowest version of the leg above: `{}` is not a reviewed plan."""
    outcome = _run_guard("--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout={})
    assert outcome.returncode == 1


def test_errored_export_is_refused(preserved_plan: Path, tmp_path: Path) -> None:
    """Terraform's own `errored` flag must be honoured, not just its exit code."""
    document = _no_op_plan_json()
    document["errored"] = True
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=document
    )
    assert outcome.returncode == 1
    assert "errored" in outcome.stdout


def test_incomplete_export_is_refused(preserved_plan: Path, tmp_path: Path) -> None:
    """`complete: false` means Terraform could not determine every change."""
    document = _no_op_plan_json()
    document["complete"] = False
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=document
    )
    assert outcome.returncode == 1
    assert "not complete" in outcome.stdout


@pytest.mark.parametrize("invalid", [None, {}, "", 0])
def test_invalid_resource_changes_type_is_refused(
    preserved_plan: Path, tmp_path: Path, invalid: object
) -> None:
    """Present-but-wrong-type must refuse, where absent passes.

    `document.get("resource_changes") or []` — the idiom this replaced — collapses
    every value here into "no entries", so a structurally invalid document was
    indistinguishable from a correct one. Absence is now legitimate, which makes
    type checking the only thing separating the two.
    """
    document = _no_op_plan_json()
    document["resource_changes"] = invalid  # type: ignore[assignment]
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=document
    )
    assert outcome.returncode == 1, (
        f"resource_changes={invalid!r} is malformed, not empty, and must be refused."
    )
    assert "not a list" in outcome.stdout


@pytest.mark.parametrize("invalid", [None, {}, "", 0])
def test_invalid_resource_drift_type_is_refused(
    preserved_plan: Path, tmp_path: Path, invalid: object
) -> None:
    """Same reasoning as above for the drift collection."""
    document = _no_op_plan_json()
    document["resource_drift"] = invalid  # type: ignore[assignment]
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=document
    )
    assert outcome.returncode == 1
    assert "not a list" in outcome.stdout


def test_invalid_change_actions_type_is_refused(
    preserved_plan: Path, tmp_path: Path
) -> None:
    """A change entry whose `actions` is not a list cannot be read as no-op."""
    document = _no_op_plan_json()
    document["resource_changes"][0]["change"]["actions"] = "no-op"
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=document
    )
    assert outcome.returncode == 1


# ---------------------------------------------------------------------------
# The action and drift refusals: what the plan would actually do.
# ---------------------------------------------------------------------------


def test_ordinary_full_plan_is_refused(preserved_plan: Path, tmp_path: Path) -> None:
    """An ordinary full plan proposes destroying the undeclared add-on — refuse it.

    This is the mistake with the worst consequence: on the real cluster that
    resource is CoreDNS. The runbook warns against it in prose; this makes the
    warning enforceable.
    """
    document = _no_op_plan_json()
    document["resource_changes"][0]["change"]["actions"] = ["delete"]
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=document
    )
    assert outcome.returncode == 1, "a plan proposing a delete must be refused."
    assert "FAIL" in outcome.stdout
    assert NEWER in outcome.stdout


def test_refreshed_deletion_is_refused(preserved_plan: Path, tmp_path: Path) -> None:
    """A drift entry proposing delete means applying would drop state. Refuse."""
    document = _no_op_plan_json()
    document["resource_drift"] = [{"address": NEWER, "change": {"actions": ["delete"]}}]
    outcome = _run_guard(
        "--plan-file", str(preserved_plan), tmp_path=tmp_path, stdout=document
    )
    assert outcome.returncode == 1, "a drift deletion must be refused."
    assert "drift" in outcome.stdout.lower()


# ---------------------------------------------------------------------------
# Preservation between the plan's two state members.
# ---------------------------------------------------------------------------


def test_resource_loss_is_refused(tmp_path: Path) -> None:
    """A managed address present before and absent after is the core loss case."""
    prior, result_state = _preserved_pair()
    result_state["resources"] = [
        record for record in result_state["resources"] if record["type"] != NEWER.split(".")[0]
    ]
    plan = _write_plan(tmp_path / "loss.tfplan", prior, result_state)

    outcome = _run_guard(
        "--plan-file", str(plan), tmp_path=tmp_path, stdout=_no_op_plan_json()
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

    outcome = _run_guard(
        "--plan-file", str(plan), tmp_path=tmp_path, stdout=_no_op_plan_json()
    )
    assert outcome.returncode == 1, "a changed identity field must be refused."
    assert f"{STALE}.id" in outcome.stdout
    assert "lt-9999999999999999" not in outcome.stdout, (
        "the guard must name the field that differs, never the value: state carries "
        "attribute values and these logs are readable."
    )


def test_plan_without_state_members_is_refused(tmp_path: Path) -> None:
    """If the plan carries no state, preservation cannot be established. Refuse.

    Directly guards the decorative-gate failure mode: treating a missing member as
    "nothing lost" would let every preservation leg vacuously pass.
    """
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "nostate.tfplan", prior, result_state, omit="tfstate-prev")

    outcome = _run_guard(
        "--plan-file", str(plan), tmp_path=tmp_path, stdout=_no_op_plan_json()
    )
    assert outcome.returncode == 1
    assert "tfstate-prev" in outcome.stdout


def test_plan_json_passed_as_plan_file_is_refused(tmp_path: Path) -> None:
    """A JSON export is not a saved plan. Refuse rather than misread it."""
    plan_json = _write_json(tmp_path / "only.json", _no_op_plan_json())
    outcome = _run_guard(
        "--plan-file", str(plan_json), tmp_path=tmp_path, stdout=_no_op_plan_json()
    )
    assert outcome.returncode == 1
    assert "not a readable plan file" in outcome.stdout


def test_state_with_no_managed_resources_is_refused(tmp_path: Path) -> None:
    """An empty managed set satisfies every comparison, so it must not read as a pass.

    This is the vacuous-pass case: with no managed resources, "nothing is missing"
    and "no identity changed" are both trivially true, and the guard would report
    preservation it never checked.
    """
    empty = _state([])
    plan = _write_plan(tmp_path / "empty.tfplan", empty, empty)

    outcome = _run_guard(
        "--plan-file", str(plan), tmp_path=tmp_path, stdout=_no_op_plan_json(())
    )
    assert outcome.returncode == 1
    assert "no managed resources" in outcome.stdout


def test_duplicate_managed_address_is_refused(tmp_path: Path) -> None:
    """One record must not be able to stand in for another that was lost.

    If the same address appears twice, a presence comparison keyed on address cannot
    tell a preserved resource from a duplicated one.
    """
    prior, result_state = _preserved_pair()
    prior["resources"].append(json.loads(json.dumps(prior["resources"][0])))
    plan = _write_plan(tmp_path / "dupe.tfplan", prior, result_state)

    outcome = _run_guard(
        "--plan-file", str(plan), tmp_path=tmp_path, stdout=_no_op_plan_json()
    )
    assert outcome.returncode == 1
    assert "more than once" in outcome.stdout


def test_invalid_state_resources_type_is_refused(tmp_path: Path) -> None:
    """A state member whose `resources` is not a list is malformed, not empty."""
    prior, result_state = _preserved_pair()
    prior["resources"] = {}  # type: ignore[assignment]
    plan = _write_plan(tmp_path / "badstate.tfplan", prior, result_state)

    outcome = _run_guard(
        "--plan-file", str(plan), tmp_path=tmp_path, stdout=_no_op_plan_json()
    )
    assert outcome.returncode == 1
    assert "not a list" in outcome.stdout


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

    outcome = _run_guard(
        "--plan-file", str(plan), tmp_path=tmp_path, stdout=_no_op_plan_json(())
    )
    assert outcome.returncode == 1, (
        "dropping one instance of an indexed resource must be refused; if addresses "
        "omitted the index, both instances would collide into one key and the loss "
        "would be invisible."
    )
    assert "module.vpc.aws_subnet.private[1]" in outcome.stdout


# ---------------------------------------------------------------------------
# Optional legs: the preserved snapshot and the expected count.
# ---------------------------------------------------------------------------


def test_matching_snapshot_and_count_pass(tmp_path: Path) -> None:
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "ok.tfplan", prior, result_state)
    snapshot_path = _write_json(tmp_path / "pre.tfstate", prior)

    outcome = _run_guard(
        "--plan-file", str(plan), "--preserved-snapshot", str(snapshot_path),
        "--expect-resources", "2",
        tmp_path=tmp_path, stdout=_no_op_plan_json(),
    )
    assert outcome.returncode == 0, f"refused a matching case: {outcome.stdout}"


def test_snapshot_missing_address_is_refused(tmp_path: Path) -> None:
    """A resource dropped BEFORE the plan was generated is absent from both members.

    Without this comparison it would pass every other leg, because the guard would
    never have seen it. That is why the snapshot from step 1 is worth taking.
    """
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "snap.tfplan", prior, result_state)

    snapshot = _state(
        prior["resources"]
        + [_resource("aws_s3_bucket.extra", schema_version=0, resource_id="adp-extra")]
    )
    snapshot_path = _write_json(tmp_path / "pre.tfstate", snapshot)

    outcome = _run_guard(
        "--plan-file", str(plan), "--preserved-snapshot", str(snapshot_path),
        tmp_path=tmp_path, stdout=_no_op_plan_json(),
    )
    assert outcome.returncode == 1
    assert "aws_s3_bucket.extra" in outcome.stdout


def test_snapshot_identity_change_is_refused(tmp_path: Path) -> None:
    """The baseline must bind identities, not just address names.

    Address equality alone accepts a state whose record was re-pointed at a
    different object before the plan was generated: same address, different `id`.
    Nothing later in the sequence would notice, because both of the plan's own
    members already carry the new value.
    """
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "snapid.tfplan", prior, result_state)

    snapshot = json.loads(json.dumps(prior))
    for record in snapshot["resources"]:
        if record["type"] == STALE.split(".")[0]:
            record["instances"][0]["attributes"]["id"] = "lt-original00000000"
    snapshot_path = _write_json(tmp_path / "pre.tfstate", snapshot)

    outcome = _run_guard(
        "--plan-file", str(plan), "--preserved-snapshot", str(snapshot_path),
        tmp_path=tmp_path, stdout=_no_op_plan_json(),
    )
    assert outcome.returncode == 1, (
        "a snapshot whose identity differs from the plan's prior state must be "
        "refused: the address survived but it no longer names the same object."
    )
    assert f"{STALE}.id" in outcome.stdout
    assert "lt-original00000000" not in outcome.stdout, (
        "identity mismatches must name the field, never the value."
    )


def test_snapshot_from_a_different_lineage_is_refused(tmp_path: Path) -> None:
    """A snapshot of some other state is not this migration's baseline.

    Terraform's `lineage` identifies the state itself. Comparing against a snapshot
    from a different lineage produces a reassuring result about the wrong state.
    """
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "lineage.tfplan", prior, result_state)

    snapshot = _state(
        json.loads(json.dumps(prior["resources"])),
        lineage="11111111-1111-1111-1111-111111111111",
    )
    snapshot_path = _write_json(tmp_path / "pre.tfstate", snapshot)

    outcome = _run_guard(
        "--plan-file", str(plan), "--preserved-snapshot", str(snapshot_path),
        tmp_path=tmp_path, stdout=_no_op_plan_json(),
    )
    assert outcome.returncode == 1
    assert "lineage" in outcome.stdout


def test_wrong_expected_count_is_refused(tmp_path: Path) -> None:
    """The count root established on the real state (136) must be assertable.

    If the plan under review does not carry it, either it is the wrong plan or state
    moved since — both need re-review rather than an apply.
    """
    prior, result_state = _preserved_pair()
    plan = _write_plan(tmp_path / "count.tfplan", prior, result_state)

    outcome = _run_guard(
        "--plan-file", str(plan), "--expect-resources", "136",
        tmp_path=tmp_path, stdout=_no_op_plan_json(),
    )
    assert outcome.returncode == 1
    assert "136" in outcome.stdout
