"""Mixed-age platform state must stay inspectable — Issue #5831, EPIC #3959.

## Why this suite exists, and why the sibling suite was not enough

`test_platform_provider_constraint.py` checks the AWS provider *floor*. That floor is
necessary and it fixed the defect it was aimed at: provider 5.x publishes no resource
identity schema for `aws_eks_addon`, so a state record carrying the identity fields a
newer provider writes could not be serialised to JSON at all.

It was not sufficient, and this suite exists because of how that was found out. With the
raised floor in place, a freshly generated *targeted* plan against the real dev state
still could not be exported:

    Failed to marshal plan to json: error marshaling prior state: schema version 0
    for aws_launch_template.gvisor_nodes in state does not match version 1 from the
    provider

The first attempt at this issue reproduced the defect with a fixture containing only the
newer add-on record. That fixture passes on the raised floor while the real path fails.
A regression test whose fixture is younger than the real state certifies the wrong
thing, so the fixture here deliberately carries records of **two different ages**.

## The mechanism, which is the part worth knowing

Terraform upgrades a resource's state schema only for resources that are **in scope for
the run**. `-target` puts everything else out of scope, so untargeted resources keep
their recorded `schema_version` — and `terraform show -json` serialises *all* prior
state, not just the targeted subset. So targeting is what turns a stale record into an
export failure.

That also means **no provider version can satisfy both records at once**, which is why
the answer is a state migration rather than a different bound. The table below is
recorded observed research across provider versions; the resolved provider's half of
it is what `test_resolved_provider_cannot_satisfy_both_records` asserts, since a test
can only inspect the version actually initialised:

| Provider        | `aws_launch_template` schema | `aws_eks_addon` identity schema |
|-----------------|------------------------------|---------------------------------|
| 5.100.0         | 0 — matches the old record   | absent — breaks the add-on      |
| 6.0.0 – 6.14.0  | 0 — matches the old record   | absent                          |
| 6.16.0 +        | 1 — mismatches the old record| present from 6.42.0             |

Reading the old launch-template record needs schema 0; reading the add-on identity needs
6.42.0+, which ships schema 1. The requirements are disjoint.

## What is asserted

Offline, always:

* the fixture really does carry both ages of record — a drifted fixture silently stops
  reproducing the defect it exists for;
* the runbook documents the refresh-only migration as a prerequisite, and distinguishes
  it from an ordinary full apply.

With Terraform present, against the committed fixture, all credential-free:

* a targeted plan over mixed-age state **fails** to export, naming the stale resource;
* the same plan exports once that record is at the provider's schema version, which is
  what the refresh-only migration achieves;
* a full-scope plan exports, because every resource is in scope and gets upgraded;
* the full-scope plan proposes **deleting** the undeclared add-on — the reason the
  migration must be a saved `-refresh-only` plan and never an ordinary full apply;
* the step-4 preservation guard, run with this real binary against plans Terraform
  actually wrote, refuses both an unexportable plan and an ordinary full plan. That
  linkage is what the guard's own suite cannot show: it stubs `terraform show`.

## Scope: this suite mutates nothing

Every Terraform invocation runs in a `tmp_path` copy with `-refresh=false`, against a
synthetic state record, with credential discovery disabled. No AWS call, no real state,
no apply. The `-refresh-only` *apply* leg of the documented sequence is deliberately
**not** executed here: it writes state, so it is root-operated, and the assertion this
suite makes about it is that the runbook describes it — not that a test performed it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "platform-mixed-state-5831"
STATE_FIXTURE = FIXTURE_DIR / "mixed-age-state.json"
CONFIG_FIXTURE = FIXTURE_DIR / "main.tf"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "platform-aws-provider-floor.md"
# The step-4 preservation guard. Its logic is covered offline in
# test_refresh_only_migration_guard.py; the two legs near the end of this file drive
# it with the real terraform binary against plans Terraform actually produced.
MIGRATION_GUARD = REPO_ROOT / "platform" / "scripts" / "refresh_only_migration_guard.py"

# The constraint the platform root carries. Kept as a literal so this suite reproduces
# against the provider the real configuration resolves, not a floating "latest".
PLATFORM_CONSTRAINT = ">= 6.42.0, < 7.0.0"

STALE_RESOURCE = "aws_launch_template.gvisor_nodes"
NEWER_RESOURCE = "aws_eks_addon.coredns"
TARGETED_ADDRESS = "aws_eks_cluster.main"

# Provider downloads and plans are far slower than the 60s default in script-tests.yml.
TERRAFORM_TIMEOUT = 600

def _terraform_missing_reason() -> str | None:
    """Why the Terraform legs cannot run, or None if they can.

    Skipping locally is a convenience. Skipping **in CI** would make this gate
    decorative: the fixture-shape assertions would pass, the check would go green,
    and the behaviour the suite exists to pin would never have been exercised. So
    under `CI` a missing binary is a failure, not a skip — if the workflow's
    setup-terraform step is ever dropped, that must be loud.
    """
    if shutil.which("terraform") is not None:
        return None
    if os.environ.get("CI"):
        pytest.fail(
            "terraform is not on PATH but CI is set. The Terraform-dependent legs of "
            "this suite are the ones that reproduce #5831; skipping them in CI would "
            "report green having checked only the fixture's shape. Restore the "
            "setup-terraform step in script-tests.yml."
        )
    return "terraform binary not on PATH; the offline assertions still run"


terraform_required = pytest.mark.skipif(
    _terraform_missing_reason() is not None,
    reason="terraform binary not on PATH; the offline assertions still run",
)


def _offline_env() -> dict[str, str]:
    """Environment with AWS credential discovery disabled.

    This suite's plans must not be satisfiable by an ambient identity. The worker and
    the CI pool both run with real credentials available, so an accidental live call
    would not merely be slow — it would mean a test that claims to be offline is
    reading a real account. Stripping the variables makes that impossible rather than
    unlikely, and matches the pattern `platform-upgrade-tests.yml` uses for its
    mock-provider jobs.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("AWS_", "BOTO_"))
    }
    env.update(
        {
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_REGION": "us-east-1",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
    )
    return env


def _terraform(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["terraform", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=TERRAFORM_TIMEOUT,
        env=_offline_env(),
        check=False,
    )


@pytest.fixture(scope="module")
def initialised_workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the fixture with the platform's real constraint, `init` already run.

    Module-scoped because `init` downloads a provider; the per-test legs each copy the
    pristine state record back in, so they cannot leak state into one another.
    """
    workdir = tmp_path_factory.mktemp("mixed-state")
    config = CONFIG_FIXTURE.read_text().replace(
        "AWS_VERSION_CONSTRAINT", PLATFORM_CONSTRAINT
    )
    (workdir / "main.tf").write_text(config)

    result = _terraform(workdir, "init", "-input=false")
    if result.returncode != 0:
        # Same reasoning as a missing binary, and the same split. Locally, no
        # registry access is a convenience skip. In CI it is a failure: every leg
        # that reproduces #5831 depends on this init, so skipping here would report
        # green having checked only the fixture's shape — the exact decorative-gate
        # outcome this suite was added to prevent. A provider that stops resolving
        # must be loud, not silently unexercised.
        message = (
            "terraform init could not resolve the AWS provider "
            f"(no registry access?); stderr: {result.stderr[-400:]}"
        )
        if os.environ.get("CI"):
            pytest.fail(
                f"{message}\n\nCI is set, so this is a failure rather than a skip: the "
                "Terraform legs of this suite are the ones that reproduce the defect, "
                "and they cannot run without a resolved provider."
            )
        pytest.skip(message)
    return workdir


def _load_state() -> dict:
    return json.loads(STATE_FIXTURE.read_text())


def _place_state(workdir: Path, *, migrate_stale: bool) -> None:
    """Write the fixture state into `workdir`, optionally at the provider's schema.

    `migrate_stale=True` stands in for the *outcome* of the documented refresh-only
    migration — the stale record at the provider's current schema version — without
    performing an apply. It changes only `schema_version`, so the two legs differ in
    exactly the one field under test.
    """
    state = _load_state()
    if migrate_stale:
        stale_type = STALE_RESOURCE.split(".", 1)[0]
        for resource in state["resources"]:
            if resource["type"] == stale_type:
                resource["instances"][0]["schema_version"] = 1
    (workdir / "terraform.tfstate").write_text(json.dumps(state, indent=2))


def _plan_and_export(
    workdir: Path, *, targeted: bool
) -> subprocess.CompletedProcess[str]:
    plan_args = ["plan", "-refresh=false", "-input=false", "-out=tf.plan"]
    if targeted:
        plan_args.append(f"-target={TARGETED_ADDRESS}")

    planned = _terraform(workdir, *plan_args)
    assert planned.returncode == 0, (
        "the plan itself must succeed — this defect is specifically about the JSON "
        f"export step, not about planning. stderr: {planned.stderr[-600:]}"
    )
    return _terraform(workdir, "show", "-json", "tf.plan")


# ---------------------------------------------------------------------------
# Fixture integrity. These run with or without Terraform: if the fixture stops
# carrying two ages of record, every leg below is asserting against fiction.
# ---------------------------------------------------------------------------


def test_fixture_files_exist() -> None:
    assert STATE_FIXTURE.is_file(), f"{STATE_FIXTURE} must exist."
    assert CONFIG_FIXTURE.is_file(), f"{CONFIG_FIXTURE} must exist."


def test_fixture_carries_both_ages_of_state_record() -> None:
    """The fixture must hold an older *and* a newer record, or it reproduces nothing.

    This is the assertion that pins the lesson from this issue. A fixture containing
    only the newer add-on record passes on the raised provider floor while the real
    targeted plan still fails — so "the tests are green" would again mean nothing.
    """
    state = _load_state()
    records = {
        f"{resource['type']}.{resource['name']}": resource["instances"][0]
        for resource in state["resources"]
    }

    assert STALE_RESOURCE in records, (
        f"the fixture must contain {STALE_RESOURCE}, the resource whose state record is "
        "OLDER than the provider's schema. Without it the fixture cannot reproduce the "
        "targeted-export failure observed against the real dev state (#5831)."
    )
    assert records[STALE_RESOURCE].get("schema_version") == 0, (
        f"{STALE_RESOURCE} must stay at schema_version 0 — that mismatch against the "
        "provider's version 1 IS the defect. Raising it here would make the suite pass "
        "by deleting its own subject."
    )

    assert NEWER_RESOURCE in records, (
        f"the fixture must contain {NEWER_RESOURCE}, the resource whose record is NEWER "
        "than provider 5.x understands."
    )
    newer = records[NEWER_RESOURCE]
    assert "identity" in newer and newer["identity"], (
        f"{NEWER_RESOURCE} must carry the resource identity fields a newer provider "
        "writes; their absence is what provider 5.x could not serialise."
    )
    assert "namespace_config" in newer["attributes"], (
        f"{NEWER_RESOURCE} must carry namespace_config, the attribute added in provider "
        "6.42.0 that sets the floor the sibling suite guards."
    )


def test_fixture_does_not_declare_the_add_on_it_records() -> None:
    """The config must NOT declare the add-on, mirroring the observed mismatch.

    The real platform source declares no `aws_eks_addon "coredns"` while the real state
    records one. That mismatch is why a full-scope plan proposes deleting it, which is
    the reason the documented migration must be `-refresh-only` and not a full apply.
    Declaring it in the fixture would quietly remove the hazard under test.
    """
    config = CONFIG_FIXTURE.read_text()
    assert not re.search(r'resource\s+"aws_eks_addon"', config), (
        "the reproduction config must not declare aws_eks_addon: the observed "
        "state/source mismatch is the subject of the full-scope destroy assertion."
    )


# ---------------------------------------------------------------------------
# The provider-bound question, answered with schemas rather than release notes.
# ---------------------------------------------------------------------------


@terraform_required
def test_resolved_provider_cannot_satisfy_both_records(
    initialised_workdir: Path,
) -> None:
    """The *resolved* provider satisfies the newer record and not the older one.

    Scope, stated precisely because the previous name overclaimed: this inspects only
    the provider that the platform's current constraint resolves. It does not, and
    cannot, enumerate every published version. What it establishes is that the one
    version in use declares the add-on identity schema the newer record needs (so it
    is required) AND a launch-template schema version above what the older record
    carries (so it cannot avoid that mismatch) — i.e. the conflict is present in the
    configuration as shipped, which is what makes the state migration necessary here.

    The cross-version comparison that shows the requirements are disjoint across the
    5.x/6.x range is separately labelled observed research: it is recorded in the
    runbook's Section 5a table, read from `terraform providers schema -json` per
    version at the time of investigation, and is not re-derived by this assertion.
    """
    result = _terraform(initialised_workdir, "providers", "schema", "-json")
    assert result.returncode == 0, f"providers schema failed: {result.stderr[-400:]}"

    provider = json.loads(result.stdout)["provider_schemas"][
        "registry.terraform.io/hashicorp/aws"
    ]
    identities = provider.get("resource_identity_schemas", {})
    stale_type, newer_type = STALE_RESOURCE.split(".")[0], NEWER_RESOURCE.split(".")[0]

    assert newer_type in identities, (
        f"the resolved provider must publish a resource identity schema for {newer_type}; "
        "without it the newer record cannot be serialised at all, which was the original "
        f"defect. Floor is {PLATFORM_CONSTRAINT}."
    )

    stale_schema_version = provider["resource_schemas"][stale_type]["version"]
    recorded = _load_state()
    recorded_version = next(
        resource["instances"][0].get("schema_version", 0)
        for resource in recorded["resources"]
        if resource["type"] == stale_type
    )
    assert stale_schema_version > recorded_version, (
        f"this suite assumes the resolved provider's {stale_type} schema version "
        f"({stale_schema_version}) is above the recorded one ({recorded_version}) — that "
        "gap is what the state migration closes. If the provider ever ships a version "
        "equal to the record's, re-derive the documented sequence: the premise changed."
    )


# ---------------------------------------------------------------------------
# The four export legs.
# ---------------------------------------------------------------------------


@terraform_required
def test_targeted_plan_over_mixed_age_state_cannot_export(
    initialised_workdir: Path,
) -> None:
    """The defect, reproduced: a targeted plan succeeds but its export fails.

    Both halves matter. The plan returning 0 is why this stayed invisible until review —
    nothing looks wrong until the step that makes a saved plan inspectable is reached.
    """
    _place_state(initialised_workdir, migrate_stale=False)
    exported = _plan_and_export(initialised_workdir, targeted=True)

    assert exported.returncode != 0, (
        "a targeted plan over mixed-age state must FAIL to export. If this passes, "
        "either the provider changed its schema version or the fixture drifted — "
        "re-derive the migration sequence in the runbook before relaxing this."
    )
    combined = exported.stdout + exported.stderr
    assert STALE_RESOURCE in combined and "schema version" in combined, (
        "the export failure must name the stale resource and its schema mismatch; "
        f"got: {combined[-500:]}"
    )


@terraform_required
def test_targeted_plan_exports_once_the_stale_record_is_migrated(
    initialised_workdir: Path,
) -> None:
    """Migrating the stale record — and nothing else — makes the targeted export work.

    This is what the documented refresh-only migration buys, and it isolates the cause:
    the only difference from the failing leg is one `schema_version` field.
    """
    _place_state(initialised_workdir, migrate_stale=True)
    exported = _plan_and_export(initialised_workdir, targeted=True)

    assert exported.returncode == 0, (
        "with the stale record at the provider's schema version, the targeted plan must "
        f"export. stderr: {exported.stderr[-600:]}"
    )
    doc = json.loads(exported.stdout)
    assert "resource_changes" in doc, (
        "the exported plan must carry resource_changes — that key is what the scoped-plan "
        "guard reads to enforce the narrow resource-action scope."
    )


@terraform_required
def test_full_scope_plan_exports_because_every_resource_is_in_scope(
    initialised_workdir: Path,
) -> None:
    """Scope, not refresh, is the operative variable.

    A full-scope plan over the *same un-migrated* state exports fine, because every
    resource is in scope and so every record is schema-upgraded in memory. This is the
    mechanism behind the whole defect: targeting is what leaves a record stale. Stated
    as a test so the explanation in the runbook is checked, not just asserted.
    """
    _place_state(initialised_workdir, migrate_stale=False)
    exported = _plan_and_export(initialised_workdir, targeted=False)

    assert exported.returncode == 0, (
        "a full-scope plan over mixed-age state must export: every resource is in scope, "
        f"so every record is upgraded in memory. stderr: {exported.stderr[-600:]}"
    )


@terraform_required
def test_full_scope_plan_would_destroy_the_undeclared_add_on(
    initialised_workdir: Path,
) -> None:
    """Why the migration must be `-refresh-only` and never an ordinary full apply.

    The full-scope plan exports, which could make an ordinary full apply look like a
    tempting way to normalise state. It is not: because the source declares no add-on
    while state records one, that same plan proposes **deleting** it. On the real
    cluster that resource is CoreDNS. This test is the guard on that advice — if the
    runbook ever drifts toward "just run a full apply", this is the assertion that
    should have stopped it.
    """
    _place_state(initialised_workdir, migrate_stale=False)
    exported = _plan_and_export(initialised_workdir, targeted=False)
    assert exported.returncode == 0, f"export failed: {exported.stderr[-400:]}"

    actions = {
        change["address"]: change["change"]["actions"]
        for change in json.loads(exported.stdout)["resource_changes"]
    }
    assert "delete" in actions.get(NEWER_RESOURCE, []), (
        f"a full-scope plan must be shown to propose deleting {NEWER_RESOURCE}, the "
        f"resource state records but the source does not declare. Got {actions}. If this "
        "no longer holds, the runbook's central warning needs re-deriving."
    )


# ---------------------------------------------------------------------------
# The step-4 guard, driven by the REAL terraform binary against a REAL saved plan.
#
# `test_refresh_only_migration_guard.py` covers the guard's logic with synthetic
# saved-plan ZIPs and a stubbed `terraform show`, which is what lets it run in the
# fast lane. What it cannot establish is that the guard works on a plan Terraform
# actually produced: that it invokes the right binary in the right directory, reads
# a real ZIP's members, and reacts to a real export failure rather than a simulated
# exit code. These two legs close that gap, because this suite has a binary.
#
# Both use ordinary `-refresh=false` plans. A real `-refresh-only` plan cannot be
# generated offline -- it exists to query the provider -- so the *permitted* case
# stays synthetic and stays root's to verify on the real backend. What is checkable
# here is the linkage and the refusals.
# ---------------------------------------------------------------------------


def _run_migration_guard(
    workdir: Path, plan_name: str, *extra: str
) -> subprocess.CompletedProcess[str]:
    """Run the step-4 guard from `workdir`, exactly as the runbook's step 4 does."""
    return subprocess.run(
        [
            sys.executable,
            str(MIGRATION_GUARD),
            "--plan-file",
            str(workdir / plan_name),
            *extra,
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=TERRAFORM_TIMEOUT,
        env=_offline_env(),
        check=False,
    )


@terraform_required
def test_migration_guard_refuses_a_real_plan_whose_export_fails(
    initialised_workdir: Path,
) -> None:
    """The guard must refuse the #5831 failure itself, exporting the plan on its own.

    This is the leg that links the guard to a real artifact. The plan file here is
    one Terraform wrote, over un-migrated mixed-age state, and its export genuinely
    fails -- the same failure root hit. The guard runs `terraform show -json` itself
    and must refuse on that non-zero exit, with no caller asserting success.

    The previous interface could not be checked this way at all: it took a
    caller-supplied JSON plus `--show-exit-code`, so a passing result proved only
    that the caller had typed 0.
    """
    _place_state(initialised_workdir, migrate_stale=False)
    planned = _terraform(
        initialised_workdir,
        "plan",
        "-refresh=false",
        "-input=false",
        f"-target={TARGETED_ADDRESS}",
        "-out=guard-fail.tfplan",
    )
    assert planned.returncode == 0, f"the plan must succeed: {planned.stderr[-400:]}"

    outcome = _run_migration_guard(initialised_workdir, "guard-fail.tfplan")

    assert outcome.returncode == 1, (
        "the guard must refuse a saved plan it cannot export. Passing here would mean "
        "an unexportable plan -- the exact #5831 blocker -- reads as reviewed. "
        f"stdout: {outcome.stdout[-400:]}"
    )
    assert "terraform show -json" in outcome.stdout and "exited" in outcome.stdout
    assert "schema version" not in outcome.stdout, (
        "the guard must not reproduce Terraform's message: it quotes state, and these "
        "logs are readable."
    )


@terraform_required
def test_migration_guard_reads_a_real_saved_plan_and_refuses_its_actions(
    initialised_workdir: Path,
) -> None:
    """On a real exportable plan, the guard reads it and refuses the proposed actions.

    Complements the leg above: here the export succeeds, so the guard gets past it
    and must then read the real ZIP's `tfstate`/`tfstate-prev` members and judge the
    plan's contents. The plan is an ordinary full one, which proposes deleting the
    undeclared add-on, so the correct outcome is a refusal naming that resource --
    the same judgement the runbook's Section 5c warns about in prose.

    Together these two legs establish that the guard functions on Terraform's own
    output. Neither establishes that any real plan is safe to apply: the permitted
    case needs a genuine `-refresh-only` plan, which cannot be produced offline.
    """
    _place_state(initialised_workdir, migrate_stale=True)
    planned = _terraform(
        initialised_workdir, "plan", "-refresh=false", "-input=false", "-out=guard-ok.tfplan"
    )
    assert planned.returncode == 0, f"the plan must succeed: {planned.stderr[-400:]}"

    with zipfile.ZipFile(initialised_workdir / "guard-ok.tfplan") as archive:
        members = set(archive.namelist())
    assert {"tfstate", "tfstate-prev"} <= members, (
        "a real saved plan must carry both state members the guard reads; if Terraform "
        f"ever stops writing them the guard's approach needs revisiting. Got {members}."
    )

    outcome = _run_migration_guard(initialised_workdir, "guard-ok.tfplan")

    assert outcome.returncode == 1, (
        "an ordinary full plan proposes destroying the undeclared add-on and must be "
        f"refused. stdout: {outcome.stdout[-400:]}"
    )
    assert NEWER_RESOURCE in outcome.stdout, (
        "the refusal must name the resource whose deletion is proposed, so an operator "
        f"can see what they were about to lose. Got: {outcome.stdout[-400:]}"
    )


@terraform_required
def test_real_saved_plan_prior_state_records_no_lineage(
    initialised_workdir: Path,
) -> None:
    """Pin the metadata shape a REAL saved plan carries, because the guard depends on it.

    Terraform writes `tfstate-prev` with `lineage: ""` and `serial: 0`, while `tfstate`
    carries the state's actual lineage and serial. Root's second false refusal came
    from the guard reading that empty string as a *different* lineage and rejecting a
    genuine baseline.

    The guard now treats an empty lineage as absent metadata, which is only correct if
    this is really Terraform's shape. Asserting it against a plan Terraform wrote is
    what makes that assumption checkable: if a future version starts populating
    `tfstate-prev`'s lineage, this fails loudly instead of the guard silently skipping
    a comparison it could have made.
    """
    _place_state(initialised_workdir, migrate_stale=True)
    planned = _terraform(
        initialised_workdir, "plan", "-refresh=false", "-input=false", "-out=shape.tfplan"
    )
    assert planned.returncode == 0, f"the plan must succeed: {planned.stderr[-400:]}"

    with zipfile.ZipFile(initialised_workdir / "shape.tfplan") as archive:
        prior = json.loads(archive.read("tfstate-prev"))
        result = json.loads(archive.read("tfstate"))

    assert prior.get("lineage") == "", (
        "a real plan's tfstate-prev is expected to record no lineage. If Terraform now "
        f"populates it, the guard should compare it. Got {prior.get('lineage')!r}."
    )
    assert prior.get("serial") == 0, (
        f"tfstate-prev is expected to carry serial 0. Got {prior.get('serial')!r}."
    )
    assert result.get("lineage"), (
        "tfstate must carry the real non-empty lineage — it is the only member the "
        "guard can bind a snapshot's lineage against."
    )


# The no-change plan below uses `terraform_data`, a built-in with no provider and no
# API calls, rather than the AWS fixture. Two reasons, both necessary:
#
# * The AWS fixture cannot produce a real PASS. Its state records an add-on the source
#   does not declare, so a full plan proposes a delete; and a `-target`ed plan is
#   marked `complete: false` by Terraform, which the guard correctly refuses. Verified
#   both ways -- so neither shape can reach the snapshot leg.
# * `terraform_data` can be applied offline, which is what makes a genuine
#   `tfstate-prev`/`tfstate` pair -- with Terraform's own blank prior lineage -- exist
#   to check the guard's PASS path against.
NO_CHANGE_CONFIG = """\
terraform {
  required_providers {}
}

resource "terraform_data" "alpha" {
  input = "alpha-value"
}

resource "terraform_data" "beta" {
  input = "beta-value"
}
"""


@terraform_required
def test_migration_guard_passes_a_real_no_change_plan_against_its_own_state(
    tmp_path: Path,
) -> None:
    """The guard's PASS path, on an artifact Terraform wrote, with a real snapshot.

    The strongest available check on root's second false refusal. Everything here is
    real: Terraform applies the config, the state file becomes the preserved snapshot,
    and Terraform writes a saved plan proposing nothing. The blank `tfstate-prev`
    lineage is therefore genuine rather than synthesised, and a refusal would reproduce
    root's blocker on a real artifact.

    This is also the only leg in either suite that exercises the guard's PASS with
    `--preserved-snapshot` against real Terraform output. It does NOT show the platform
    migration is safe -- different config, no AWS, no refresh-only. It shows the
    snapshot comparison does not reject a true baseline.
    """
    workdir = tmp_path / "nochange"
    workdir.mkdir()
    (workdir / "main.tf").write_text(NO_CHANGE_CONFIG)

    initialised = _terraform(workdir, "init", "-input=false")
    assert initialised.returncode == 0, (
        "`terraform_data` is built in, so init needs no registry: a failure here is "
        f"not a network issue. stderr: {initialised.stderr[-400:]}"
    )
    applied = _terraform(workdir, "apply", "-auto-approve", "-input=false")
    assert applied.returncode == 0, f"apply must succeed: {applied.stderr[-400:]}"

    snapshot = workdir / "pre-migration.tfstate"
    shutil.copyfile(workdir / "terraform.tfstate", snapshot)

    planned = _terraform(
        workdir, "plan", "-refresh=false", "-input=false", "-out=nochange.tfplan"
    )
    assert planned.returncode == 0, f"the plan must succeed: {planned.stderr[-400:]}"

    with zipfile.ZipFile(workdir / "nochange.tfplan") as archive:
        prior = json.loads(archive.read("tfstate-prev"))
        result = json.loads(archive.read("tfstate"))
    assert prior.get("lineage") == "" and result.get("lineage"), (
        "this leg is only meaningful if the real pair carries the blank-prior/"
        f"non-empty-result shape. Got {prior.get('lineage')!r} / "
        f"{result.get('lineage')!r}."
    )

    outcome = _run_migration_guard(
        workdir, "nochange.tfplan", "--preserved-snapshot", str(snapshot)
    )

    assert "lineage" not in outcome.stdout, (
        "the plan's own originating state must never be refused as a foreign lineage. "
        "That was root's reported false refusal, reproduced here on a real artifact. "
        f"stdout: {outcome.stdout[-600:]}"
    )
    assert outcome.returncode == 0, (
        "a real plan proposing no changes, checked against the exact state it was "
        f"generated from, must pass. stdout: {outcome.stdout[-600:]}"
    )


# ---------------------------------------------------------------------------
# The runbook must carry the sequence. A mechanism nobody can follow is not a fix.
# ---------------------------------------------------------------------------


def test_runbook_documents_the_refresh_only_migration_sequence() -> None:
    """The supported sequence must be written down, in order, with its guardrail.

    The deliverable for #5831 is a reproducible rollout, not only a provider constraint.
    Asserted by content because the runbook is the only artifact an operator reads, and
    because the dangerous mistake here — reaching for a full apply — is a documentation
    failure rather than a code one.
    """
    assert RUNBOOK.is_file(), f"{RUNBOOK} must exist."
    body = RUNBOOK.read_text()

    assert "-refresh-only" in body, (
        "the runbook must name `terraform plan -refresh-only`: it is the only reviewed "
        "route that normalises state schema without proposing resource changes."
    )
    assert STALE_RESOURCE.split(".")[0] in body, (
        f"the runbook must name {STALE_RESOURCE.split('.')[0]}, the resource type whose "
        "stale record blocks the targeted export, so an operator can recognise the error."
    )
    assert re.search(r"full apply|ordinary full", body), (
        "the runbook must explicitly distinguish the refresh-only state migration from "
        "an ordinary full apply, which would propose destroying the undeclared add-on."
    )


def test_runbook_steps_are_not_invoked_with_a_failure_swallowing_handler() -> None:
    """No step may be invoked as `step || echo ...`. That does not stop the sequence.

    `||` *handles* the failure, so the compound command succeeds, `set -e` has nothing
    to act on, and the next line runs — including, eventually, the apply. The runbook
    previously invoked all six steps this way, so every check in it was advisory while
    reading as fail-stop. Asserted by content because the defect is invisible: the
    output says STOP and the script exits 0.
    """
    body = RUNBOOK.read_text()
    offenders = [
        line.strip()
        for line in body.splitlines()
        if re.match(r"^\w+ *\|\| *echo", line.strip())
    ]
    assert not offenders, (
        "these step invocations swallow the failure they claim to report; use "
        f"`|| exit 1` or the `&&` chain instead: {offenders}"
    )


def test_runbook_orchestration_chain_cannot_reach_a_later_step_after_a_refusal() -> None:
    """Execute the runbook's chaining pattern and prove a refusal is terminal.

    The content assertion above says the bad pattern is gone; this says the replacement
    actually works. The runbook chains its steps with `&&` inside one function, so this
    runs that exact shape with a deliberately failing first step and asserts that
    neither a later step nor a sentinel standing in for the apply is ever reached.

    Without this, "the sequence stops" is a claim about shell semantics that nobody
    checked — and the previous version of the runbook is proof that such claims can be
    wrong.
    """
    body = RUNBOOK.read_text()
    assert re.search(r"review_migration\(\) \{", body), (
        "the runbook must define a single orchestration function; invoking steps "
        "line by line is what allowed a handled failure to fall through."
    )

    script = """
set -euo pipefail
confirm_account() { echo "REFUSED-ACCOUNT"; return 1; }
resolve_backend() { echo "REACHED-RESOLVE"; }
preserve()        { echo "REACHED-PRESERVE"; }
plan_migration()  { echo "REACHED-PLAN"; }
inspect_migration() { echo "REACHED-INSPECT"; }

review_migration() {
  confirm_account \\
    && resolve_backend \\
    && preserve \\
    && plan_migration \\
    && inspect_migration
}

if review_migration; then
  echo "REACHED-APPLY"
else
  echo "STOPPED" >&2
  exit 1
fi
"""
    outcome = subprocess.run(
        ["bash"], input=script, text=True, capture_output=True, timeout=60, check=False
    )

    assert outcome.returncode == 1, (
        "a refused first step must make the whole sequence exit non-zero. "
        f"Got {outcome.returncode}; stdout: {outcome.stdout!r}"
    )
    assert "REFUSED-ACCOUNT" in outcome.stdout, "the failing step must still report."
    for unreachable in (
        "REACHED-RESOLVE",
        "REACHED-PRESERVE",
        "REACHED-PLAN",
        "REACHED-INSPECT",
        "REACHED-APPLY",
    ):
        assert unreachable not in outcome.stdout, (
            f"{unreachable} was reached after a refusal. The chain does not stop, so "
            "every check downstream of the refusal is decorative."
        )
