#!/usr/bin/env python3
"""Fail-closed preservation check for the platform refresh-only state migration (#5831).

Why this exists
---------------
The platform's state carries records of two different ages, and no AWS provider
version can decode both (see ``docs/runbooks/platform-aws-provider-floor.md``
Section 5a). The only reviewed route that normalises them is a full-scope
``terraform plan -refresh-only``, applied from its saved file. That apply writes
**state**, not cloud resources — but a state write is exactly where a resource can
be silently dropped from management, so it must not be applied on the strength of
"the plan looked fine".

This guard is the reviewable gate in front of that apply. It answers one question:
**does this saved plan preserve every managed resource and its identity, and does
it propose no resource changes?** Anything else is a refusal.

It takes one artifact
---------------------
The only input that matters is ``--plan-file``: the saved plan itself. This script
runs ``terraform show -json`` on that exact file and checks the exit code itself,
rather than accepting a caller-supplied JSON plus an asserted "it worked". That
matters because an operator can hold a stale or unrelated export from an earlier
run: binding the JSON to the plan is what makes the review evidence about *this*
plan. The export is held in memory and not written to disk unless
``--save-json`` is passed, so the guard does not leave a second readable file
carrying resource attribute values.

Why it also reads the saved plan's state members
------------------------------------------------
The obvious approach — compare ``planned_values`` against ``prior_state`` in the
``terraform show -json`` output — does not work on this plan shape. **A
``-refresh-only`` plan's ``planned_values`` can be empty**, because it proposes no
resource changes; reading preservation from it would compare nothing to nothing
and pass. That failure mode is the whole reason this file is not a one-liner.

A saved plan file is a ZIP that carries its own state, in two members:

* ``tfstate-prev`` — state as recorded before the run.
* ``tfstate`` — state as the run would leave it, i.e. the migration's *result*,
  with schema versions upgraded.

Comparing those two is what actually establishes preservation, and it is how the
migration was verified against the real backend (all 136 managed resources, with
``id``/``arn``/``name`` unchanged). Both members are read directly from the ZIP.

An omitted optional field is not a broken export
------------------------------------------------
Terraform **omits** ``resource_changes`` from the JSON of a plan that proposes no
resource changes, which is exactly what a successful ``-refresh-only`` plan is. An
earlier version of this guard refused that document as truncated, i.e. it refused
the correct plan it exists to approve. So the distinction it now enforces is:

* *legitimately absent optional field* → zero entries, keep checking.
* *unusable document* → refusal. That means a non-zero ``terraform show`` exit, a
  body that does not parse, a body that is not a plan export (no
  ``format_version``), one Terraform itself marks ``errored`` or not ``complete``,
  or a field that is **present with the wrong type**.

The last case is why this script does not write ``document.get(k) or []``: that
expression silently accepts ``null``, ``{}`` and ``""`` as "an empty collection,
nothing to check here", which is indistinguishable from a real absence and turns a
structurally invalid document into a pass.

What each leg refuses, and why
------------------------------
Every leg must hold or this exits non-zero. There is no default-allow branch and
no ``|| true``: an *unknown* is a refusal, never a pass.

1. ``terraform show -json`` on ``--plan-file`` exits 0 and yields a usable plan
   document, per the paragraph above. A failed export is not "zero changes,
   proceed" — that reading is how a guard becomes decorative.
2. No entry in ``resource_changes`` has actions other than ``["no-op"]``. A
   ``-refresh-only`` plan that proposes creating, updating or destroying anything
   is not a state migration.
3. No ``resource_drift`` entry proposes ``delete``. A drift deletion means
   Terraform did not find the resource in the account; applying it removes the
   resource from state, which is the specific loss this guard exists to stop.
4. Both state members are structurally valid: state documents carrying at least
   one managed resource, with no address recorded twice. An empty or duplicated
   managed-instance collection would let the preservation legs pass vacuously.
5. Every managed address in ``tfstate-prev`` is still present in ``tfstate``.
6. For each of those addresses, the identity fields ``id``, ``arn`` and ``name``
   are unchanged. A changed ``id`` means state now points at a different object.
7. When ``--preserved-snapshot`` is given, the pre-migration snapshot's managed
   addresses are all present in **both** plan state members with the same identity
   fields, and no two of the three documents record *conflicting non-empty*
   lineages. Address equality alone would accept a baseline taken from a different
   state, or one whose ``id`` changed before the plan was generated.

   A real saved plan's ``tfstate-prev`` carries ``lineage: ""`` and ``serial: 0``
   while its ``tfstate`` carries the state's actual lineage and serial. That is
   Terraform's own shape, verified against a plan it wrote, not a foreign snapshot
   -- so an empty lineage is *absent metadata*, and comparing a real lineage
   against it is invalid. Only two differing non-empty values indicate a genuinely
   different state.

Limits — read these before relying on it
----------------------------------------
* It inspects a **saved plan file**, so it establishes what that plan would do to
  state. It does **not** call AWS and cannot confirm the resources exist in the
  account; leg 3 is the only signal about the account, and it is Terraform's
  observation, not this script's.
* Schema-version upgrades are **expected** between the two members — that is the
  migration's purpose — so this guard deliberately does not compare
  ``schema_version``. It compares identity and presence.
* Passing means "safe to apply *this file*". Apply that exact saved file; a freshly
  generated plan has not been checked by this run.
* It does not authorise the apply. The apply is root-operated.

Log hygiene
-----------
Plan and state files carry resource attribute values, including secrets, and
operator terminals and CI logs are readable. So this script **never** echoes plan
or state content, and never Terraform's stdout. Output is an allowlist: Terraform
addresses, action verbs, the *names* of fields that differ, and a PASS/FAIL line. A
refusal names the address and the field, never the value found.

Usage
-----
Run from the initialised working directory whose provider produced the plan (or
pass ``--chdir``), so ``terraform show`` resolves the same provider::

    terraform plan -refresh-only -out="$TASK_DIR/migrate.tfplan"

    refresh_only_migration_guard.py \\
        --plan-file "$TASK_DIR/migrate.tfplan" \\
        [--preserved-snapshot "$TASK_DIR/pre-migration.tfstate"] \\
        [--expect-resources 136]

Exit codes: 0 = pass, 1 = refused.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import zipfile
from typing import Any

# Fields compared between the two state members. These are the identity of the
# object state points at: if any changes, the migration is not preserving what it
# claims to. Deliberately a small fixed set rather than a full attribute diff --
# refresh-only legitimately updates drifted attributes, so a whole-object
# comparison would refuse correct migrations and teach operators to bypass this.
IDENTITY_FIELDS = ("id", "arn", "name")

PLAN_STATE_MEMBER = "tfstate"
PLAN_PRIOR_STATE_MEMBER = "tfstate-prev"

# `terraform show -json` on a large plan is not instant, but it is not unbounded
# either; a hang must surface as a refusal rather than a stalled review.
SHOW_TIMEOUT_SECONDS = 600


class Refused(ValueError):
    """Only constant, non-secret refusal text belongs in this exception."""


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise Refused(reason)


def require_list(document: dict[str, Any], key: str, context: str) -> list[Any]:
    """Read an optional list field, distinguishing absent from invalid.

    Absent means Terraform had no entries to report, which for a refresh-only plan
    is the expected and correct output. Present-but-not-a-list means the document
    is not what it claims to be, and must refuse -- the `or []` idiom this replaces
    collapsed `null`, `{}` and `""` into a silent pass.
    """
    if key not in document:
        return []
    value = document[key]
    require(
        isinstance(value, list),
        f"{context} has '{key}' but it is {type(value).__name__}, not a list. This "
        "document is not a usable plan export; absent would be fine, malformed is not.",
    )
    return value


def address_of(resource: dict[str, Any], instance: dict[str, Any]) -> str:
    """Terraform address for one state instance, including module and index."""
    parts = []
    module = resource.get("module")
    if module:
        parts.append(module)
    parts.append(f"{resource['type']}.{resource['name']}")
    address = ".".join(parts)
    key = instance.get("index_key")
    if key is not None:
        address += f'["{key}"]' if isinstance(key, str) else f"[{key}]"
    return address


def managed_instances(state: dict[str, Any], context: str) -> dict[str, dict[str, Any]]:
    """Map address -> instance for every *managed* resource in a state document.

    Refuses a structurally invalid document rather than returning an empty map: an
    empty map satisfies every preservation comparison trivially, so "I could not
    find any resources" and "nothing was lost" must not produce the same verdict.
    Data sources are excluded -- they are not managed, so their presence is not a
    preservation question.
    """
    require(isinstance(state, dict), f"{context} is not a JSON object.")
    resources = require_list(state, "resources", context)

    found: dict[str, dict[str, Any]] = {}
    for resource in resources:
        require(isinstance(resource, dict), f"{context} has a non-object resource entry.")
        if resource.get("mode") != "managed":
            continue
        require(
            isinstance(resource.get("type"), str) and isinstance(resource.get("name"), str),
            f"{context} has a managed resource without a string type and name.",
        )
        instances = require_list(resource, "instances", context)
        for instance in instances:
            require(
                isinstance(instance, dict),
                f"{context} has a non-object instance under "
                f"{resource['type']}.{resource['name']}.",
            )
            address = address_of(resource, instance)
            require(
                address not in found,
                f"{context} records the managed address {address} more than once. A "
                "duplicated address makes presence comparison meaningless, because one "
                "record can stand in for another that was lost.",
            )
            found[address] = instance

    require(
        found,
        f"{context} contains no managed resources. That is not evidence of "
        "preservation: every comparison against an empty set passes. Is this the "
        "right artifact?",
    )
    return found


def identity_of(instance: dict[str, Any]) -> dict[str, Any]:
    """The identity fields this guard compares, for one state instance."""
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return {}
    return {field: attributes[field] for field in IDENTITY_FIELDS if field in attributes}


def read_plan_states(plan_file: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract (prior, resulting) state documents from a saved plan file.

    A missing member is a refusal rather than an empty default: if the plan does
    not carry its own state, this guard cannot establish preservation at all, and
    silently treating that as "nothing lost" is precisely the decorative-gate
    failure mode.
    """
    try:
        with zipfile.ZipFile(plan_file) as archive:
            members = set(archive.namelist())
            for member in (PLAN_PRIOR_STATE_MEMBER, PLAN_STATE_MEMBER):
                require(
                    member in members,
                    f"saved plan {plan_file} has no '{member}' member, so preservation "
                    "cannot be established from it. Was this file produced by "
                    "`terraform plan -out=...`?",
                )
            prior = json.loads(archive.read(PLAN_PRIOR_STATE_MEMBER))
            result = json.loads(archive.read(PLAN_STATE_MEMBER))
    except zipfile.BadZipFile as exc:
        raise Refused(
            f"saved plan {plan_file} is not a readable plan file ({type(exc).__name__}). "
            "A plan JSON export is not a substitute: pass the -out file itself."
        ) from exc
    except json.JSONDecodeError as exc:
        raise Refused(
            f"a state member of {plan_file} is not valid JSON ({type(exc).__name__})."
        ) from exc
    except OSError as exc:
        raise Refused(f"cannot read saved plan {plan_file}: {type(exc).__name__}.") from exc
    return prior, result


def export_plan_json(
    plan_file: str, terraform: str, chdir: str, save_to: str = ""
) -> dict[str, Any]:
    """Run `terraform show -json` on this exact plan file and parse the result.

    The guard produces its own export rather than accepting one, so the document it
    reviews provably describes the plan it is about to approve. Terraform's stderr
    is never echoed: it can quote state and attribute values.
    """
    require(
        os.path.isfile(plan_file),
        f"saved plan {plan_file} does not exist or is not a file.",
    )
    try:
        completed = subprocess.run(
            [terraform, "show", "-json", os.path.abspath(plan_file)],
            cwd=chdir or None,
            capture_output=True,
            text=True,
            timeout=SHOW_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise Refused(
            f"cannot run '{terraform}': not found. This guard exports the plan itself "
            "rather than trusting a supplied JSON, so it needs the same terraform "
            "binary and initialised directory that produced the plan."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise Refused(
            f"`terraform show -json` did not finish within {SHOW_TIMEOUT_SECONDS}s."
        ) from exc
    except OSError as exc:
        raise Refused(f"cannot run '{terraform}': {type(exc).__name__}.") from exc

    require(
        completed.returncode == 0,
        f"`terraform show -json` exited {completed.returncode} on {plan_file}. The "
        "saved plan could not be exported, so it has not been reviewed. This is the "
        "#5831 failure mode itself; see the runbook Section 5a. (Terraform's message "
        "is not reproduced here because it quotes state.)",
    )

    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise Refused(
            f"`terraform show -json` exited 0 but its output does not parse "
            f"({type(exc).__name__}). A truncated export is not zero changes."
        ) from exc

    validate_plan_document(document, plan_file)

    if save_to:
        with open(save_to, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        os.chmod(save_to, 0o600)
    return document


def validate_plan_document(document: Any, source: str) -> None:
    """Refuse a document that is not a complete, successful plan export.

    Deliberately does **not** require `resource_changes`: Terraform omits it from a
    plan that proposes nothing, which is what a correct refresh-only plan is. What
    is required is evidence the export is a plan and that Terraform considered it
    finished and not errored.
    """
    require(isinstance(document, dict), f"the export of {source} is not a JSON object.")
    require(
        isinstance(document.get("format_version"), str),
        f"the export of {source} has no string 'format_version', so it is not a "
        "Terraform plan export. Was a state file or an unrelated document passed?",
    )
    for key in ("planned_values", "prior_state", "configuration"):
        if key in document:
            require(
                isinstance(document[key], dict),
                f"the export of {source} has '{key}' but it is "
                f"{type(document[key]).__name__}, not an object.",
            )
    require(
        document.get("errored") is not True,
        f"Terraform marked the export of {source} as errored. An errored plan has not "
        "been shown to propose nothing.",
    )
    if "complete" in document:
        require(
            document["complete"] is True,
            f"Terraform marked the export of {source} as not complete, so it may omit "
            "changes it could not determine. Resolve that before reviewing it.",
        )


def check_no_resource_changes(document: dict[str, Any], source: str) -> int:
    """Refuse any proposed create/update/delete. Returns the entries reviewed."""
    changes = require_list(document, "resource_changes", f"the export of {source}")

    offending = []
    for entry in changes:
        require(
            isinstance(entry, dict),
            f"the export of {source} has a non-object 'resource_changes' entry.",
        )
        change = entry.get("change")
        require(
            change is None or isinstance(change, dict),
            f"the export of {source} has a non-object 'change' under "
            f"{entry.get('address', '<no address>')}.",
        )
        actions = require_list(change or {}, "actions", f"the export of {source}")
        if actions != ["no-op"]:
            offending.append(f"{entry.get('address', '<no address>')} {actions}")

    require(
        not offending,
        "a -refresh-only migration must propose no resource changes, but this plan "
        f"proposes {len(offending)}: {'; '.join(sorted(offending))}. An ordinary full "
        "plan will fail here by design — it proposes destroying the undeclared add-on. "
        "Regenerate with -refresh-only; do not apply this file.",
    )
    return len(changes)


def check_no_drift_deletions(document: dict[str, Any], source: str) -> int:
    """Refuse drift entries proposing delete: applying those drops state."""
    drift = require_list(document, "resource_drift", f"the export of {source}")

    deletions = []
    for entry in drift:
        require(
            isinstance(entry, dict),
            f"the export of {source} has a non-object 'resource_drift' entry.",
        )
        change = entry.get("change")
        require(
            change is None or isinstance(change, dict),
            f"the export of {source} has a non-object drift 'change' under "
            f"{entry.get('address', '<no address>')}.",
        )
        actions = require_list(change or {}, "actions", f"the export of {source}")
        if "delete" in actions:
            deletions.append(entry.get("address", "<no address>"))

    require(
        not deletions,
        f"{len(deletions)} drift entry/entries propose delete: "
        f"{'; '.join(sorted(deletions))}. Terraform did not find these in the account, "
        "so applying this plan removes them from state. Investigate before applying.",
    )
    return len(drift)


def check_preservation(prior: dict[str, Any], result: dict[str, Any]) -> tuple[int, int]:
    """Refuse a dropped managed address or a changed identity field."""
    before = managed_instances(prior, "the plan's prior state (tfstate-prev)")
    after = managed_instances(result, "the plan's resulting state (tfstate)")

    missing = sorted(set(before) - set(after))
    require(
        not missing,
        f"{len(missing)} managed resource(s) present before the migration are absent "
        f"after it: {'; '.join(missing)}. Applying this plan would drop them from "
        "management.",
    )

    changed = []
    for address, instance in sorted(before.items()):
        was = identity_of(instance)
        now = identity_of(after[address])
        for field, value in was.items():
            if now.get(field) != value:
                # The field name, never the value: state carries secrets.
                changed.append(f"{address}.{field}")
    require(
        not changed,
        f"{len(changed)} identity field(s) change across the migration: "
        f"{'; '.join(changed)}. A changed id/arn/name means state would point at a "
        "different object than the one under management.",
    )
    return len(before), len(after)


def lineage_of(document: dict[str, Any]) -> str:
    """The state lineage a document records, or "" when it records none.

    A saved plan's ``tfstate-prev`` member carries ``lineage: ""`` -- Terraform
    writes the pre-plan member without lineage metadata. So an empty string means
    *not recorded*, not *a different lineage*, and must never be compared against a
    real value. A non-string is also treated as absent rather than as a mismatch;
    malformed metadata is not evidence that the state differs.
    """
    lineage = document.get("lineage")
    return lineage if isinstance(lineage, str) else ""


def check_lineage_agreement(candidates: list[tuple[str, dict[str, Any]]]) -> str:
    """Refuse if two documents record *conflicting non-empty* lineages.

    Only non-empty values carry information (see ``lineage_of``), so this compares
    the ones that are recorded and ignores the ones that are not. Returns the agreed
    lineage, or "" when none of the documents recorded one.
    """
    recorded = [(label, lineage_of(document)) for label, document in candidates]
    present = [(label, value) for label, value in recorded if value]
    if not present:
        return ""

    _, agreed = present[0]
    for label, value in present[1:]:
        require(
            value == agreed,
            f"{label} records a different state lineage than {present[0][0]}. Two "
            "documents recording conflicting non-empty lineages cannot describe the "
            "same state, so this snapshot cannot serve as this migration's baseline. "
            "(An empty lineage is absent metadata and is not compared: a real saved "
            "plan's tfstate-prev legitimately carries one.)",
        )
    return agreed


def check_snapshot(
    prior: dict[str, Any], result: dict[str, Any], snapshot_path: str
) -> int:
    """Refuse if the pre-migration snapshot and the plan's own states disagree.

    Guards two cases the plan's own members cannot show. A resource dropped before
    the plan was generated is absent from both members, so it passes every leg
    above. And a snapshot that is not actually this state's baseline -- a
    conflicting lineage, or the same address already re-pointed at another object --
    would make the comparison reassuring but meaningless, so identity and lineage
    are bound too, not just address names.

    Identity is compared against **both** plan members. ``tfstate-prev`` is the
    state the plan was generated from and ``tfstate`` is what applying it produces;
    a snapshot that agrees with one but not the other is a real discrepancy.
    """
    try:
        with open(snapshot_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
    except json.JSONDecodeError as exc:
        raise Refused(
            f"preserved snapshot {snapshot_path} does not parse ({type(exc).__name__})."
        ) from exc
    except OSError as exc:
        raise Refused(
            f"cannot read preserved snapshot {snapshot_path}: {type(exc).__name__}."
        ) from exc

    context = f"preserved snapshot {snapshot_path}"
    require(isinstance(snapshot, dict), f"{context} is not a JSON object.")

    check_lineage_agreement(
        [
            (context, snapshot),
            ("the plan's prior state (tfstate-prev)", prior),
            ("the plan's resulting state (tfstate)", result),
        ]
    )

    recorded = managed_instances(snapshot, context)
    for label, member in (
        ("the plan's prior state (tfstate-prev)", prior),
        ("the plan's resulting state (tfstate)", result),
    ):
        planned = managed_instances(member, label)

        missing = sorted(set(recorded) - set(planned))
        require(
            not missing,
            f"{len(missing)} managed resource(s) in the preserved snapshot are absent "
            f"from {label}: {'; '.join(missing)}. State changed between the snapshot "
            "and the plan; re-snapshot and re-review.",
        )

        changed = []
        for address, instance in sorted(recorded.items()):
            was = identity_of(instance)
            now = identity_of(planned[address])
            for field, value in was.items():
                if now.get(field) != value:
                    changed.append(f"{address}.{field}")
        require(
            not changed,
            f"{len(changed)} identity field(s) differ between the preserved snapshot "
            f"and {label}: {'; '.join(changed)}. State was re-pointed at a different "
            "object; investigate before applying.",
        )
    return len(recorded)


def run(args: argparse.Namespace) -> int:
    report: list[str] = []
    try:
        document = export_plan_json(
            args.plan_file, args.terraform, args.chdir, args.save_json
        )
        reviewed = check_no_resource_changes(document, args.plan_file)
        drift_entries = check_no_drift_deletions(document, args.plan_file)

        prior, result = read_plan_states(args.plan_file)
        before, after = check_preservation(prior, result)

        if args.preserved_snapshot:
            recorded = check_snapshot(prior, result, args.preserved_snapshot)
            report.append(
                f"preserved snapshot managed resources: {recorded}, matched against "
                "both plan state members"
            )

        if args.expect_resources is not None:
            require(
                before == args.expect_resources,
                f"expected {args.expect_resources} managed resources in the plan's prior "
                f"state, found {before}. Either the wrong plan is under review or state "
                "changed since that count was established.",
            )

        report.append(
            f"resource changes reported: {reviewed} entries, 0 non-no-op "
            "(Terraform omits the field entirely when it has none)"
        )
        report.append(f"drift entries: {drift_entries}, proposing delete: 0")
        report.append(f"managed resources preserved: {before} -> {after}")
    except Refused as refusal:
        for line in report:
            print(line)
        print(f"FAIL: {refusal}")
        return 1

    for line in report:
        print(line)
    print(
        "PASS: this saved plan proposes no resource changes, no drift deletions, and "
        "preserves every managed address and identity. Apply THIS file only."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--plan-file",
        required=True,
        help="The saved `terraform plan -out` file. This guard exports it itself.",
    )
    parser.add_argument(
        "--preserved-snapshot",
        default="",
        help="Optional pre-migration state snapshot. Its addresses and identity fields "
        "are compared against both plan state members; lineages are compared only "
        "where recorded (a real plan's tfstate-prev carries an empty one).",
    )
    parser.add_argument(
        "--expect-resources",
        type=int,
        default=None,
        help="Optional exact managed-resource count the plan's prior state must carry.",
    )
    parser.add_argument(
        "--chdir",
        default="",
        help="Directory to run `terraform show` from. Defaults to the current "
        "directory, which must be the initialised working directory that produced "
        "the plan.",
    )
    parser.add_argument(
        "--terraform",
        default="terraform",
        help="Terraform binary to invoke. Defaults to `terraform` on PATH; use the "
        "same version that produced the plan.",
    )
    parser.add_argument(
        "--save-json",
        default="",
        help="Optional path to write the export to, mode 600. Omitted by default so "
        "the guard leaves no extra file carrying attribute values.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
