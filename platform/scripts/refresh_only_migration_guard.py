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

Why it reads the saved plan's state members
-------------------------------------------
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

What each leg refuses, and why
------------------------------
Every leg must hold or this exits non-zero. There is no default-allow branch and
no ``|| true``: an *unknown* is a refusal, never a pass.

1. The plan JSON parses and contains ``resource_changes``. A truncated or errored
   export is not "zero changes, proceed" — that reading is how a guard becomes
   decorative.
2. No entry in ``resource_changes`` has actions other than ``["no-op"]``. A
   ``-refresh-only`` plan that proposes creating, updating or destroying anything
   is not a state migration.
3. No ``resource_drift`` entry proposes ``delete``. A drift deletion means
   Terraform did not find the resource in the account; applying it removes the
   resource from state, which is the specific loss this guard exists to stop.
4. Every managed address in ``tfstate-prev`` is still present in ``tfstate``.
5. For each of those addresses, the identity fields ``id``, ``arn`` and ``name``
   are unchanged. A changed ``id`` means state now points at a different object.
6. When ``--preserved-snapshot`` is given, the pre-migration snapshot's managed
   addresses are all present too — so a resource dropped before the plan was
   generated cannot pass by being absent from both sides of the plan.

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
or state content. Output is an allowlist: Terraform addresses, action verbs, the
*names* of fields that differ, and a PASS/FAIL line. A refusal names the address
and the field, never the value found.

Usage
-----
::

    terraform plan -refresh-only -out="$DIR/migrate.tfplan"
    terraform show -json "$DIR/migrate.tfplan" > "$DIR/migrate.json"

    refresh_only_migration_guard.py \\
        --plan-file "$DIR/migrate.tfplan" \\
        --plan-json "$DIR/migrate.json" \\
        --show-exit-code 0 \\
        [--preserved-snapshot "$DIR/pre-migration.tfstate"] \\
        [--expect-resources 136]

Exit codes: 0 = pass, 1 = refused.
"""

from __future__ import annotations

import argparse
import json
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


class Refused(ValueError):
    """Only constant, non-secret refusal text belongs in this exception."""


def require(condition: Any, reason: str) -> None:
    if not condition:
        raise Refused(reason)


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


def managed_instances(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map address -> instance for every *managed* resource in a state document.

    Data sources are excluded: they are not managed, so their presence or absence
    is not a preservation question.
    """
    found: dict[str, dict[str, Any]] = {}
    for resource in state.get("resources", []):
        if resource.get("mode") != "managed":
            continue
        for instance in resource.get("instances", []):
            found[address_of(resource, instance)] = instance
    return found


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


def load_plan_json(path: str, show_exit_code: int) -> dict[str, Any]:
    """Parse the `terraform show -json` output, refusing anything ambiguous.

    The exit code is passed in by the caller rather than inferred: an empty or
    partial file is ambiguous on its own, and this defect began as an *export*
    failure, so a failed export must refuse loudly instead of parsing to nothing.
    """
    require(
        show_exit_code == 0,
        f"`terraform show -json` exited {show_exit_code}. The saved plan could not be "
        "exported, so it has not been reviewed. This is the #5831 failure mode itself.",
    )
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise Refused(
            f"plan JSON {path} does not parse ({type(exc).__name__}). A truncated export "
            "is not zero changes."
        ) from exc
    except OSError as exc:
        raise Refused(f"cannot read plan JSON {path}: {type(exc).__name__}.") from exc

    require(isinstance(document, dict), f"plan JSON {path} is not a JSON object.")
    require(
        "resource_changes" in document,
        f"plan JSON {path} has no 'resource_changes' key. Absent is not empty: a plan "
        "document without it has not been shown to propose nothing.",
    )
    return document


def check_no_resource_changes(document: dict[str, Any]) -> list[str]:
    """Refuse any proposed create/update/delete. Returns the reviewed addresses."""
    changes = document.get("resource_changes") or []
    require(isinstance(changes, list), "'resource_changes' is not a list.")

    offending = []
    for entry in changes:
        actions = (entry.get("change") or {}).get("actions") or []
        if actions != ["no-op"]:
            offending.append(f"{entry.get('address', '<no address>')} {actions}")

    require(
        not offending,
        "a -refresh-only migration must propose no resource changes, but this plan "
        f"proposes {len(offending)}: {'; '.join(sorted(offending))}. An ordinary full "
        "plan will fail here by design — it proposes destroying the undeclared add-on. "
        "Regenerate with -refresh-only; do not apply this file.",
    )
    return [entry.get("address", "") for entry in changes]


def check_no_drift_deletions(document: dict[str, Any]) -> int:
    """Refuse drift entries proposing delete: applying those drops state."""
    drift = document.get("resource_drift") or []
    require(isinstance(drift, list), "'resource_drift' is not a list.")

    deletions = [
        entry.get("address", "<no address>")
        for entry in drift
        if "delete" in ((entry.get("change") or {}).get("actions") or [])
    ]
    require(
        not deletions,
        f"{len(deletions)} drift entry/entries propose delete: "
        f"{'; '.join(sorted(deletions))}. Terraform did not find these in the account, "
        "so applying this plan removes them from state. Investigate before applying.",
    )
    return len(drift)


def check_preservation(
    prior: dict[str, Any], result: dict[str, Any]
) -> tuple[int, int]:
    """Refuse a dropped managed address or a changed identity field."""
    before = managed_instances(prior)
    after = managed_instances(result)

    missing = sorted(set(before) - set(after))
    require(
        not missing,
        f"{len(missing)} managed resource(s) present before the migration are absent "
        f"after it: {'; '.join(missing)}. Applying this plan would drop them from "
        "management.",
    )

    changed = []
    for address, instance in sorted(before.items()):
        was = instance.get("attributes") or {}
        now = after[address].get("attributes") or {}
        for field in IDENTITY_FIELDS:
            if field in was and was.get(field) != now.get(field):
                # The field name, never the value: state carries secrets.
                changed.append(f"{address}.{field}")
    require(
        not changed,
        f"{len(changed)} identity field(s) change across the migration: "
        f"{'; '.join(changed)}. A changed id/arn/name means state would point at a "
        "different object than the one under management.",
    )
    return len(before), len(after)


def check_snapshot(prior: dict[str, Any], snapshot_path: str) -> int:
    """Refuse if the pre-migration snapshot holds addresses the plan does not.

    Guards the case where a resource was already dropped before the plan was
    generated: it would be absent from both of the plan's own members and so pass
    every leg above.
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

    recorded = managed_instances(snapshot)
    missing = sorted(set(recorded) - set(managed_instances(prior)))
    require(
        not missing,
        f"{len(missing)} managed resource(s) in the preserved snapshot are absent from "
        f"the plan's prior state: {'; '.join(missing)}. State changed between the "
        "snapshot and the plan; re-snapshot and re-review.",
    )
    return len(recorded)


def run(args: argparse.Namespace) -> int:
    report: list[str] = []
    try:
        document = load_plan_json(args.plan_json, args.show_exit_code)
        reviewed = check_no_resource_changes(document)
        drift_entries = check_no_drift_deletions(document)

        prior, result = read_plan_states(args.plan_file)
        before, after = check_preservation(prior, result)

        if args.preserved_snapshot:
            recorded = check_snapshot(prior, args.preserved_snapshot)
            report.append(f"preserved snapshot managed resources: {recorded}")

        if args.expect_resources is not None:
            require(
                before == args.expect_resources,
                f"expected {args.expect_resources} managed resources in the plan's prior "
                f"state, found {before}. Either the wrong plan is under review or state "
                "changed since that count was established.",
            )

        report.append(f"resource changes proposed: {len(reviewed)} reviewed, 0 non-no-op")
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
        "--plan-file", required=True, help="The saved `terraform plan -out` file."
    )
    parser.add_argument(
        "--plan-json", required=True, help="`terraform show -json <saved plan>` output."
    )
    parser.add_argument(
        "--show-exit-code",
        type=int,
        required=True,
        help="Exit code of the `terraform show -json` that produced --plan-json.",
    )
    parser.add_argument(
        "--preserved-snapshot",
        default="",
        help="Optional pre-migration state snapshot to compare addresses against.",
    )
    parser.add_argument(
        "--expect-resources",
        type=int,
        default=None,
        help="Optional exact managed-resource count the plan's prior state must carry.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
