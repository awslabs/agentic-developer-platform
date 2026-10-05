"""Substitute manifest placeholders from environment DATA — Issue #5042 (U3).

## Why this replaced a `sed` pipeline

The previous render step built its `sed` invocation by interpolating GitHub expressions
directly into the shell:

    sed -e "s|REPLACE_WITH_NAMESPACE|${{ steps.config.outputs.namespace }}|g" ...

`${{ }}` interpolation is textual substitution into the script *before* the shell parses it,
so the value is not data at that point — it is source code. A value containing `|` ends the
`sed` expression; one containing a quote or `$(…)` reaches the shell. These values come from
SSM parameters, which an operator can edit by hand, so "the value is trusted because
Terraform wrote it" is not an assumption the render step gets to make.

The values here arrive as **environment variables**, which are data at every layer: the
workflow assigns them with `env:`, this process reads them with `os.environ`, and the
substitution is a Python string operation. Nothing is parsed as shell or as a `sed` script.

## Three properties beyond safe substitution

1.  **Every placeholder present in a manifest must have a value.** An unsubstituted
    `REPLACE_WITH_…` would be applied literally — `kubectl` accepts a namespace or annotation
    value of `REPLACE_WITH_NAMESPACE` without complaint.
2.  **Every value supplied must be used — unless the component that would use it is one the
    release lock cannot yet build.** See below; this is the interesting case.
3.  **The result still parses as YAML.** This is checked by re-parsing rather than by
    blacklisting characters in the input, because the values here legitimately contain YAML
    metacharacters: a role ARN is `arn:aws:iam::123456789012:role/…` and an image reference is
    `registry/repo@sha256:…`. An early draft of this script rejected any `:` and therefore
    refused every real ARN — a character blacklist cannot distinguish `arn:aws` (a plain
    scalar containing a colon, which YAML accepts) from `key: value` (a mapping). Parsing can.

## Why an unused value is sometimes expected, and when it stops being

A value nothing consumes normally means a placeholder was renamed on one side only, which
renders successfully while silently dropping an identity. That is an error.

The default full rollout lane retains the lock-dependent guard: a control-plane value
may go unused while its owning image is pending, but promotion makes that an error.
Publishing an image does not itself create ADP deployment manifests. The explicit
`skypilot` lane renders the currently shipped SkyPilot-only object set, supplies only
its four inputs, and rejects control-plane inputs rather than silently ignoring them.
Adding control-plane manifests requires the full rollout lane and its complete inputs.
The upstream `src/` manifests are not rendered or applied by either lane.

"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import yaml

PLACEHOLDER_RE = re.compile(r"REPLACE_WITH_[A-Z0-9_]+")

# placeholder -> environment variable carrying its value, grouped by LANE.
#
# WHY LANES RATHER THAN ONE FLAT TABLE
#
# The full rollout, SkyPilot-only rollout (`k8s/`), and migration (`migrations/`)
# lanes consume different placeholders, and property 2 above — every value
# supplied must be consumed — is what makes lanes necessary rather than cosmetic. A single
# flat table would mean the rollout supplies the migration's values, nothing consumes them,
# and the rollout either fails or has to exempt them; the exemption is then a permanent hole
# that would also hide a genuinely dropped rollout value.
#
# Keeping the sets separate preserves the property inside each lane, and gives a second
# check for free: a placeholder from the wrong lane appearing in a manifest is reported as
# "no value supplied" rather than silently rendered by whichever lane happened to define it.
LANE_PLACEHOLDER_ENV = {
    "rollout": {
        "REPLACE_WITH_NAMESPACE": "SP_NAMESPACE",
        "REPLACE_WITH_SKYPILOT_NAMESPACE": "SP_SKYPILOT_NAMESPACE",
        "REPLACE_WITH_CONTROL_PLANE_ROLE_ARN": "SP_CONTROL_PLANE_ROLE_ARN",
        "REPLACE_WITH_SKYPILOT_ROLE_ARN": "SP_SKYPILOT_ROLE_ARN",
        "REPLACE_WITH_SKYPILOT_IMAGE": "SP_SKYPILOT_IMAGE",
        "REPLACE_WITH_DATABASE_SECRET_NAME": "SP_DATABASE_SECRET_NAME",
        "REPLACE_WITH_JWT_SECRET_NAME": "SP_JWT_SECRET_NAME",
        "REPLACE_WITH_AWS_REGION": "SP_AWS_REGION",
    },
    "skypilot": {
        "REPLACE_WITH_SKYPILOT_NAMESPACE": "SP_SKYPILOT_NAMESPACE",
        "REPLACE_WITH_SKYPILOT_ROLE_ARN": "SP_SKYPILOT_ROLE_ARN",
        "REPLACE_WITH_SKYPILOT_IMAGE": "SP_SKYPILOT_IMAGE",
        "REPLACE_WITH_AWS_REGION": "SP_AWS_REGION",
    },
    "migration": {
        "REPLACE_WITH_NAMESPACE": "SP_NAMESPACE",
        "REPLACE_WITH_MIGRATION_IMAGE": "SP_MIGRATION_IMAGE",
        "REPLACE_WITH_MIGRATION_RUN_ID": "SP_MIGRATION_RUN_ID",
        "REPLACE_WITH_DATABASE_SCHEMA": "SP_DATABASE_SCHEMA",
        "REPLACE_WITH_AWS_REGION": "SP_AWS_REGION",
    },
}

# Values that become identifiers inside a larger string, where the generic checks are not
# enough.
#
# The important one is the schema. It is substituted into
# `PGOPTIONS: "-c search_path=REPLACE_WITH_DATABASE_SCHEMA"`, and a value of
# `superplane,public` would render valid YAML, pass every structural check here, and quietly
# put `public` back on the search_path — dissolving the boundary that replaced the SQL text
# scan. `FORBIDDEN_IN_VALUE` cannot catch it, because a comma breaks neither the document nor
# the value: it changes the MEANING of the setting the value lands in.
#
# The run id is constrained because it becomes part of a Job's `metadata.name`; failing here
# names the cause, rather than surfacing as an API-server rejection of a name nobody chose.
PLACEHOLDER_VALUE_PATTERN = {
    "REPLACE_WITH_DATABASE_SCHEMA": (
        re.compile(r"^[a-z_][a-z0-9_]{0,62}$"),
        "a single lowercase Postgres identifier. It is substituted into a `search_path`, so "
        "a comma-separated value would silently add a schema an unqualified statement could "
        "then resolve into — which is exactly the boundary this value establishes",
    ),
    "REPLACE_WITH_MIGRATION_RUN_ID": (
        re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"),
        "a DNS-1123 label: it becomes part of the migration Job's metadata.name",
    ),
}

# Characters that break the DOCUMENT rather than the value.
#
# Deliberately short. A newline or carriage return ends the YAML line these placeholders sit
# on, so a value carrying one restructures the document — and that failure surfaces as an
# unrelated parse error somewhere else in the file. A `#` would start a comment, truncating
# the value silently.
#
# `:`, `/`, `@`, `.` and `-` are NOT here: every real value uses them
# (`arn:aws:iam::…:role/x`, `registry/repo@sha256:…`, `adp/dev/superplane/database`). The
# structural check is the YAML re-parse below, not a character blacklist.
FORBIDDEN_IN_VALUE = ("\n", "\r", "#")

# Which lock image each placeholder's consumer belongs to.
#
# A placeholder listed here may legitimately go unused while its image is `pending_images` in
# the release lock, because there is no manifest to consume it yet. Anything NOT listed here
# must be consumed unconditionally.
PLACEHOLDER_OWNING_IMAGE = {
    # The control-plane placeholders share the API image as their promotion gate.
    "REPLACE_WITH_NAMESPACE": "superplane-api",
    "REPLACE_WITH_CONTROL_PLANE_ROLE_ARN": "superplane-api",
    "REPLACE_WITH_DATABASE_SECRET_NAME": "superplane-api",
    "REPLACE_WITH_JWT_SECRET_NAME": "superplane-api",
}


def _pending_images(lock_path: Path) -> set[str]:
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) or {}
    if not isinstance(lock, dict):
        raise ValueError(f"{lock_path} did not parse as a mapping")
    pending = lock.get("pending_images") or {}
    if not isinstance(pending, dict):
        raise ValueError(f"{lock_path}: `pending_images` is not a mapping")
    return set(pending)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--lock-file",
        required=True,
        type=Path,
        help=(
            "The release lock. Read to decide whether an unused value is expected (its "
            "component is not yet buildable) or a defect (its component is deployable)."
        ),
    )
    parser.add_argument(
        "--lane",
        default="rollout",
        choices=sorted(LANE_PLACEHOLDER_ENV),
        help=(
            "Which placeholder set this render supplies. Separate sets keep 'every supplied "
            "value is consumed' meaningful in each lane — see LANE_PLACEHOLDER_ENV."
        ),
    )
    args = parser.parse_args(argv)

    placeholder_env = LANE_PLACEHOLDER_ENV[args.lane]
    if args.lane == "skypilot":
        outside_lane = set(LANE_PLACEHOLDER_ENV["rollout"].values()) - set(
            placeholder_env.values()
        )
        supplied = sorted(name for name in outside_lane if os.environ.get(name))
        if supplied:
            print(
                "::error::control-plane values supplied to the SkyPilot-only lane: "
                + ", ".join(supplied)
            )
            return 1

    try:
        pending = _pending_images(args.lock_file)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"::error::could not read the release lock at {args.lock_file}: {exc}")
        return 1

    if not args.source_dir.is_dir():
        print(f"::error::source directory {args.source_dir} does not exist")
        return 1

    values: dict[str, str] = {}
    for placeholder, env_name in placeholder_env.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            print(
                f"::error::{env_name} is unset or empty, so {placeholder} has no value. The "
                f"placeholder would be applied literally."
            )
            return 1
        bad = [
            repr(character) for character in FORBIDDEN_IN_VALUE if character in value
        ]
        if bad:
            print(
                f"::error::{env_name} contains {', '.join(bad)}, which would restructure the "
                f"YAML line {placeholder} sits on. Refusing to render."
            )
            return 1
        constraint = PLACEHOLDER_VALUE_PATTERN.get(placeholder)
        if constraint is not None:
            pattern, expectation = constraint
            if not pattern.fullmatch(value):
                print(
                    f"::error::{env_name} is {value!r}, which is not {expectation}. Refusing "
                    f"to render."
                )
                return 1
        values[placeholder] = value

    args.output_dir.mkdir(parents=True, exist_ok=True)
    # Clear stale renders: a leftover file from a previous run would be validated and applied
    # as though it belonged to this one.
    for existing in args.output_dir.iterdir():
        if existing.is_file():
            existing.unlink()

    used: set[str] = set()
    rendered_count = 0

    for path in sorted(args.source_dir.iterdir()):
        if path.suffix not in {".yaml", ".yml"}:
            continue
        text = path.read_text(encoding="utf-8")

        found = set(PLACEHOLDER_RE.findall(text))
        unknown = found - set(values)
        if unknown:
            print(
                f"::error::{path.name} contains placeholder(s) with no value supplied: "
                f"{', '.join(sorted(unknown))}. Add them to PLACEHOLDER_ENV in "
                f"render_manifests.py rather than leaving them to be applied literally."
            )
            return 1
        used |= found

        for placeholder, value in values.items():
            text = text.replace(placeholder, value)

        residual = PLACEHOLDER_RE.search(text)
        if residual:
            print(f"::error::{path.name}: {residual.group(0)} survived substitution")
            return 1

        # The structural check. A substituted value that broke the document would otherwise
        # be caught later — by the rendered-manifest guard, or by kubectl — as a parse error
        # attributed to the manifest rather than to the value that caused it.
        try:
            list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            print(
                f"::error::{path.name} no longer parses as YAML after substitution, so a "
                f"substituted value restructured the document: {exc}"
            )
            return 1

        (args.output_dir / path.name).write_text(text, encoding="utf-8")
        rendered_count += 1
        print(f"Rendered {path.name}")

    if rendered_count == 0:
        print(f"::error::no manifests found in {args.source_dir}; nothing was rendered")
        return 1

    unused = set(values) - used
    expected_unused, unexpected_unused = set(), set()
    for placeholder in unused:
        owner = PLACEHOLDER_OWNING_IMAGE.get(placeholder)
        if owner and owner in pending:
            expected_unused.add(placeholder)
        else:
            unexpected_unused.add(placeholder)

    if unexpected_unused:
        print("::error::value(s) supplied but consumed by no manifest:")
        for placeholder in sorted(unexpected_unused):
            owner = PLACEHOLDER_OWNING_IMAGE.get(placeholder)
            if owner:
                # The arming case: the lock resolved this image, so its manifests are now
                # owed. Naming the image makes the next step obvious.
                print(
                    f"  - {placeholder}: its component '{owner}' is no longer pending in the "
                    f"release lock, so the manifests that consume it are now expected to exist"
                )
            else:
                print(
                    f"  - {placeholder}: either it was renamed in the manifests without "
                    f"updating PLACEHOLDER_ENV, or the manifest that used it was removed"
                )
        return 1

    if expected_unused:
        print(
            f"Values not consumed because their component is not yet buildable "
            f"({', '.join(sorted(expected_unused))}) — the release lock records the "
            f"corresponding image as pending with no digest. This is reported rather than "
            f"passed over silently, and becomes an error once the lock resolves it."
        )

    consumed = len(values) - len(expected_unused)
    print(
        f"Rendered {rendered_count} manifest(s); {consumed}/{len(values)} values consumed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
