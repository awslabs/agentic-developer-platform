#!/usr/bin/env python3
"""Fail-closed guard for a *scoped* platform-infra apply (defect #5006).

Why this exists
---------------
``platform-infra-apply.yml`` applies the whole ``platform/infra`` root module.
Activating the Auto Mode network-policy controller (merged default-off in
#5000) needs exactly one resource created --
``module.eks.kubernetes_config_map.amazon_vpc_cni[0]`` -- but the same root
module currently carries unrelated pending changes, including an ECR repository
*replacement* nobody approved (#5003). So the only available route could not be
taken, and a merged security control stayed inert.

The route this guard makes safe is a named ``scope`` input on that workflow: a
``-target``ed plan saved to a file, this guard over that saved plan, then
``terraform apply <that same file>``. The guard is what turns "narrow by
intent" into "narrow by proof".

The predicate, and why each leg is here
---------------------------------------
Every leg below must hold or the process exits non-zero. There is no ``||
true`` and no default-allow branch anywhere in this file; an *unknown* is a
refusal, never a pass.

1. ``terraform show -json`` exited 0, and its output parses as JSON containing
   a ``resource_changes`` key. A truncated or errored show is not "zero changes,
   proceed" -- that reading is how a guard becomes decorative. The exit code is
   passed in by the caller rather than inferred, because an empty file is
   ambiguous on its own.
2. Exactly one entry in ``resource_changes`` has ``change.actions`` other than
   ``["no-op"]``. Collateral of *any* kind refuses (the #5003 protection).
3. That entry's ``address`` matches the scope's single hardcoded address.
4. Enablement actions are exactly ``["create"]`` or ``["update"]``. Rollback
   permits only ``["delete"]`` with explicit destructive confirmation and an
   exact match of the enabled **before** object. Exact list
   comparison is deliberate: it refuses ``["delete","create"]`` **and**
   ``["create","delete"]`` without enumerating replacement spellings. #5002
   exists because a gate grepped for ``will be destroyed`` and therefore never
   saw ``must be replaced``.
5. The object being written is the reviewed one, field by field: metadata name,
   namespace, and an exact-match ``data`` map. An extra key in ``data`` is a
   different ConfigMap than the one reviewed, so it refuses.
6. No ``after_unknown`` coverage over any asserted field. If the plan does not
   yet know what it will write, the reviewed plan is not the applied plan.
7. The effective AWS identity's account matches the resolved deployment target
   (and the operator's requested account, when they named one). Guarding the
   plan's *content* is worthless if it is applied to a different account.
8. The scope name itself is recognised. An unrecognised scope refuses rather
   than falling through to a full apply -- so adding a workflow choice option
   without teaching this file about it fails closed.

Log hygiene
-----------
Terraform plan JSON can carry resource attribute values, including secrets, and
these workflow logs are readable. So this script **never** echoes plan JSON.
Output is an allowlist: for each non-no-op change, its ``address`` and its
``actions``, both of which are Terraform addresses and action verbs rather than
attribute values -- plus a PASS/FAIL line and a reason. Refusal reasons name the
field that failed, never the value found.

Usage
-----
::

    verify_scoped_plan.py --scope network-policy-controller \\
        --plan-json /tmp/scoped-plan.json --show-exit-code 0 \\
        --resolved-account 879318057152 --identity-account 879318057152 \\
        [--requested-account 879318057152]

    verify_scoped_plan.py --mode validate-scope --scope full

Exit codes: 0 = pass, 1 = refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

# The one address each scope is permitted to touch, plus the exact object it is
# permitted to write. Hardcoded literals, deliberately: the workflow selects a
# key of this table, it never interpolates an operator-supplied target address
# into a Terraform command. A free-text target input would turn the workflow
# into a general-purpose "apply anything unaudited" primitive, which is worse
# than the problem this solves.
#
# Enablement and rollback have separate action/object contracts. Rollback also
# requires the workflow's existing explicit destructive-apply confirmation.
SCOPES: dict[str, dict[str, Any]] = {
    "network-policy-controller": {
        "address": "module.eks.kubernetes_config_map.amazon_vpc_cni[0]",
        "allowed_actions": (["create"], ["update"]),
        "metadata": {"name": "amazon-vpc-cni", "namespace": "kube-system"},
        "data": {"enable-network-policy-controller": "true"},
        "object_field": "after",
    },
    "network-policy-controller-rollback": {
        "address": "module.eks.kubernetes_config_map.amazon_vpc_cni[0]",
        "allowed_actions": (["delete"],),
        "metadata": {"name": "amazon-vpc-cni", "namespace": "kube-system"},
        "data": {"enable-network-policy-controller": "true"},
        "object_field": "before",
    },
}

# The scope that means "no scoping at all": the workflow's pre-existing full
# apply path. Valid as a workflow input, never valid as a guard subject.
FULL_SCOPE = "full"

NO_OP = ["no-op"]


class Refused(Exception):
    """A guard leg failed. The message names the failing field, not its value."""


def load_plan(path: str, show_exit_code: int) -> dict[str, Any]:
    """Return the parsed plan JSON, or raise Refused.

    ``show_exit_code`` is the exit status of the ``terraform show -json`` that
    produced ``path``. It is threaded through instead of being guessed, because
    a zero-byte file cannot be distinguished from "show failed" after the fact,
    and guessing wrong in the permissive direction is a silent bypass.
    """
    if show_exit_code != 0:
        raise Refused(
            f"terraform show -json exited {show_exit_code}; "
            "the plan could not be read, so it cannot be approved"
        )

    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise Refused(f"plan JSON could not be read: {exc.strerror}") from exc

    if not raw.strip():
        raise Refused("plan JSON is empty; refusing (an empty plan is not 'no changes')")

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refused(f"plan JSON does not parse (line {exc.lineno})") from exc

    if not isinstance(doc, dict):
        raise Refused("plan JSON is not a JSON object")

    if "resource_changes" not in doc:
        # An absent change list cannot prove the required single-resource change.
        raise Refused("plan JSON has no 'resource_changes' key; refusing")

    changes = doc["resource_changes"]
    if not isinstance(changes, list):
        raise Refused("'resource_changes' is not a list; refusing")

    return doc


def non_no_op_changes(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Every resource change whose actions are not exactly ["no-op"]."""
    out = []
    for entry in doc["resource_changes"]:
        if not isinstance(entry, dict):
            raise Refused("a resource_changes entry is not an object; refusing")
        change = entry.get("change")
        if not isinstance(change, dict):
            raise Refused(
                f"resource_changes entry {_addr(entry)} has no readable 'change'; refusing"
            )
        actions = change.get("actions")
        if not isinstance(actions, list):
            raise Refused(
                f"resource_changes entry {_addr(entry)} has no readable 'actions'; refusing"
            )
        if actions != NO_OP:
            out.append(entry)
    return out


def _addr(entry: dict[str, Any]) -> str:
    """The entry's address, for log lines. Addresses are not attribute values."""
    addr = entry.get("address")
    return addr if isinstance(addr, str) else "<no address>"


def _actions(entry: dict[str, Any]) -> list[str]:
    return entry["change"]["actions"]


def check_accounts(
    resolved: str, identity: str, requested: str | None
) -> None:
    """Refuse unless the account about to be applied to is the intended one."""
    resolved = (resolved or "").strip()
    identity = (identity or "").strip()
    requested = (requested or "").strip()

    if any(len(value) != 12 or not value.isascii() or not value.isdigit()
           for value in (resolved, identity)):
        raise Refused("resolved and identity accounts must be 12 digits; refusing")
    if requested and (len(requested) != 12 or not requested.isascii() or not requested.isdigit()):
        raise Refused("requested account must be 12 digits; refusing")
    if resolved != identity:
        raise Refused(
            f"account mismatch: resolved deployment target {resolved} but the "
            f"effective AWS identity is in {identity}; refusing"
        )
    # An omitted requested account is legitimate: load-deploy-config then
    # resolves it from the config file or the runtime identity. A *supplied*
    # one that disagrees is an operator targeting error and must refuse.
    if requested and requested != resolved:
        raise Refused(
            f"account mismatch: operator requested {requested} but the run "
            f"resolved to {resolved}; refusing"
        )


def _has_unknown(value: Any) -> bool:
    """Unknown leaves or malformed unknownness structures fail closed."""
    if value is False:
        return False
    if isinstance(value, dict):
        return any(_has_unknown(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_unknown(v) for v in value)
    return True


def check_asserted_unknowns(unknown: Any) -> None:
    if not isinstance(unknown, dict):
        raise Refused("missing or unreadable 'after_unknown'; refusing")
    data = unknown.get("data", {})
    if not (data is False or isinstance(data, dict)) or _has_unknown(data):
        raise Refused("'data' is unknown or unreadable at plan time; refusing")
    metadata = unknown.get("metadata", False)
    if metadata is False:
        return
    if (not isinstance(metadata, list) or len(metadata) != 1
            or not isinstance(metadata[0], dict)):
        raise Refused("metadata unknownness is unreadable; refusing")
    # generation/resource_version/uid are provider-computed on a normal create.
    # Only the asserted identity paths must be known, not the entire block.
    for field in ("name", "namespace"):
        if metadata[0].get(field, False) is not False:
            raise Refused(f"metadata {field} is unknown at plan time; refusing")


def verify_change(entry: dict[str, Any], spec: dict[str, Any]) -> None:
    """Assert one resource change is exactly the reviewed one."""
    address = _addr(entry)
    if address != spec["address"]:
        raise Refused(
            f"unexpected address {address}; this scope permits only {spec['address']}"
        )

    actions = _actions(entry)
    if actions not in spec["allowed_actions"]:
        allowed = " or ".join(json.dumps(a) for a in spec["allowed_actions"])
        raise Refused(
            f"{address} has actions {json.dumps(actions)}; this scope permits "
            f"only {allowed}"
        )

    change = entry["change"]

    object_field = spec["object_field"]
    if object_field == "after":
        check_asserted_unknowns(change.get("after_unknown"))
    elif "after" not in change or change["after"] is not None:
        raise Refused("rollback must remove the object completely; refusing")

    after = change.get(object_field)
    if not isinstance(after, dict):
        raise Refused(f"{address}: plan has no readable '{object_field}' object; refusing")

    metadata = after.get("metadata")
    if not isinstance(metadata, list) or len(metadata) != 1 or not isinstance(metadata[0], dict):
        raise Refused(f"{address}: plan has no readable metadata block; refusing")

    for field, expected in spec["metadata"].items():
        if metadata[0].get(field) != expected:
            # The expected value is a literal from this file, not plan content,
            # so naming it leaks nothing. The found value is NOT echoed.
            raise Refused(
                f"{address}: metadata {field} is not {expected!r}; refusing"
            )

    data = after.get("data")
    if data != spec["data"]:
        # Exact match, so a missing key, a changed value and an *extra* key all
        # refuse. An extra key is a different ConfigMap than the reviewed one.
        raise Refused(
            f"{address}: 'data' does not exactly match the reviewed controller setting "
            f"({sorted(spec['data'])} -> expected values); refusing"
        )


def summarise(changes: list[dict[str, Any]]) -> list[str]:
    """Allowlisted summary lines: address + actions only, never values."""
    if not changes:
        return ["  (no non-no-op changes in plan)"]
    return [
        f"  {_addr(e)}  actions={json.dumps(_actions(e))}" for e in changes
    ]


def run(args: argparse.Namespace) -> int:
    scope = args.scope or ""

    if args.mode == "validate-scope":
        # Guards the workflow's own branch selection: an option added to the
        # dispatch input without a SCOPES entry must refuse, not silently take
        # the full-apply path.
        if scope == FULL_SCOPE or scope in SCOPES:
            print(f"Scope '{scope}' is recognised.")
            return 0
        known = ", ".join([FULL_SCOPE, *sorted(SCOPES)])
        print(f"FAIL: unrecognised scope. Known scopes: {known}")
        return 1

    try:
        if scope == FULL_SCOPE:
            raise Refused(
                "scope 'full' is not a scoped apply; this guard must not be "
                "used to approve a full apply"
            )
        spec = SCOPES.get(scope)
        if spec is None:
            known = ", ".join(sorted(SCOPES))
            raise Refused(
                f"unrecognised scope; scoped applies are limited to: {known}"
            )

        if spec["object_field"] == "before" and args.confirm_destructive_apply != "yes":
            raise Refused("rollback requires explicit confirm_destructive_apply=yes; refusing")

        check_accounts(args.resolved_account, args.identity_account, args.requested_account)

        doc = load_plan(args.plan_json, args.show_exit_code)
        changes = non_no_op_changes(doc)

        print(f"Scoped apply guard -- scope '{scope}'")
        print("Non-no-op changes in the saved plan:")
        for line in summarise(changes):
            print(line)

        if len(changes) != 1:
            raise Refused(
                f"expected exactly 1 non-no-op change, found {len(changes)}; "
                "refusing (collateral in a scoped apply is not approved)"
            )

        verify_change(changes[0], spec)
    except Refused as exc:
        print(f"FAIL: {exc}")
        return 1

    print(f"PASS: the saved plan changes only {spec['address']}, as reviewed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--mode",
        choices=("verify-plan", "validate-scope"),
        default="verify-plan",
        help="verify-plan (default) audits a saved plan; validate-scope only checks the scope name.",
    )
    p.add_argument("--scope", required=True, help="Apply scope name.")
    p.add_argument("--plan-json", default="", help="Path to `terraform show -json <saved plan>` output.")
    p.add_argument(
        "--show-exit-code",
        type=int,
        default=1,
        help="Exit status of the terraform show that produced --plan-json. Defaults to 1 (refuse) when unset.",
    )
    p.add_argument("--resolved-account", default="", help="Account load-deploy-config resolved.")
    p.add_argument("--identity-account", default="", help="Account of the effective AWS identity.")
    p.add_argument("--requested-account", default="", help="Account the operator requested, if any.")
    p.add_argument("--confirm-destructive-apply", default="no", help="Existing workflow confirmation; required for rollback only.")
    return p


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
