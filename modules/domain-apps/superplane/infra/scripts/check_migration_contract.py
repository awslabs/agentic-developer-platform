"""Decide whether a Superplane migration may run at all — Issue #5042 (U3).

## What this replaces, and why the previous approach could not work

PR #5283's review (finding 6) found the migration lane establishing its database boundary by
grepping migration files for `ALTER TABLE <gateway-owned table>`. Reproduced against the
shipped pattern with the real 38-name denylist derived from the gateway's `__tablename__`
declarations:

    op.drop_table("users")                                    -> NOT caught
    op.execute('ALTER TABLE public."request_logs" DROP ...')   -> NOT caught
    op.execute("ALTER  TABLE budget_configs RENAME TO x")      -> caught (two spaces)

One of three. The scan cannot enumerate the ways to name a table: Alembic's Python API never
emits the words it looks for, schema qualification and quoting evade the pattern, and a
computed table name defeats it entirely. **A grep over SQL text is not a database boundary
and this script does not pretend otherwise.**

The boundary is enforced by the database session instead — `search_path` set to the domain
schema with `public` absent, Alembic's version table inside that schema, and the domain's own
credential. This script's job is to verify the JOB DECLARES that boundary, and to refuse when
any input the lane depends on is unavailable.

## Why refusals, not warnings

The previous lane's backup "requirement" was `echo "::warning::…"`. A warning does not stop a
step; the run continued to the upgrade. Worse, the plan step printed the strings
`alembic current` / `alembic history` without running anything, so a reader of the log saw
what looks like a checked plan.

So the rule this script enforces throughout: an input that is absent, or evidence that was
not actually obtained, produces a REFUSAL. Nothing here ever reports an echoed plan or an
unverified assertion as a checked migration or a checked backup.

## Exit codes

    0  the contract holds and every required input is present
    1  refused — a required input is unavailable, or the contract is violated

There is deliberately no "warn and continue" code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

# The domain's own schema must be the ONLY entry in search_path. `public` in particular
# must be absent: with it present, `op.drop_table("users")` resolves to the gateway's table.
FORBIDDEN_SEARCH_PATH_SCHEMAS = frozenset(
    {"public", "pg_catalog", "information_schema"}
)

# Alembic's default bookkeeping table. Sharing it means sharing the chain: two chains on one
# version table each treat the other's revisions as unknown and re-apply their own.
DEFAULT_VERSION_TABLE = "alembic_version"

# The chain lives in exactly ONE place, and revision files appearing HERE would be a second
# copy that diverges from it the first time either side changes.
#
# WHERE THAT ONE PLACE IS CHANGED WITH U22 (#5326); THE RULE DID NOT.
#
# It used to be "upstream, in the image" — outside ADP entirely. The chain is now maintained
# in this repository at `src/superplane-api/alembic/`, so the boundary this guard enforces is
# no longer ADP-vs-upstream but *this directory* vs *the component that owns the chain*. That
# is a narrower line and an easier one to cross by accident: the files are now a few
# directories away rather than in another organisation's repo, so "just drop a revision next
# to the Job" became a plausible mistake rather than an impossible one. The guard matters more
# after the transfer, not less.
#
# Both naming conventions are matched, and the short one is not hypothetical: an earlier
# version of this pattern required 4+ hex characters, which is right for Alembic's default
# 12-character hashes and wrong for THIS chain — whose revision ids are `006` and `007`, the
# very ids the duplicate-head problem is about. A copy of the actual chain would have walked
# past a check written to catch copies of it.
#
# `env.py` and `script.py.mako` are Alembic's scaffolding: their presence means a migration
# ENVIRONMENT was copied here, which is the same defect one level up from a revision file.
CHAIN_FILE_RE = re.compile(
    r"""
      ^ [0-9]{1,4} [_-] .* \.py$      # 001_initial.py, 006_add_widgets.py
    | ^ [0-9a-f]{8,} [_-] .* \.py$    # Alembic's default hash-prefixed names
    | ^ versions$                     # the chain directory itself
    | ^ env\.py$                      # Alembic's migration environment
    | ^ script\.py\.mako$             # Alembic's revision template
    | ^ alembic\.ini$                 # the chain's config; the real one lives with the chain
    """,
    re.VERBOSE,
)

REQUIRED_ENV = {
    "SUPERPLANE_DB_CONNECTION_URI",
    "SUPERPLANE_DB_SCHEMA",
    "PGOPTIONS",
    "ALEMBIC_VERSION_TABLE",
    "ALEMBIC_VERSION_TABLE_SCHEMA",
}

PLACEHOLDER_RE = re.compile(r"REPLACE_WITH_[A-Z0-9_]+")

# An inline credential inside a URI: scheme://user:password@host.
INLINE_CREDENTIAL_RE = re.compile(r"://[^/\s]*:[^/@\s]+@")


class Refusal(Exception):
    """A required input is unavailable, or the contract is violated. Never a warning."""


# ---------------------------------------------------------------------------
# Input availability. Each of these is a refusal, with the blocking unit named.
# ---------------------------------------------------------------------------


def check_chain_is_resolvable(lock: dict) -> None:
    """The chain must be single-headed and verified, per the lock.

    Not hardcoded to "blocked": when U13/#5045 repairs the chain and the lock records it,
    this unblocks by data rather than by someone remembering to edit a workflow.
    """
    schema = lock.get("schema") or {}
    if schema.get("single_head") is not True:
        observed = (schema.get("observed") or {}).get("duplicate_revision_ids") or {}
        detail = ", ".join(
            f"revision {rid!r} declared by {count} files"
            for rid, count in sorted(observed.items())
        )
        blocked = schema.get("blocked_by") or {}
        raise Refusal(
            f"The Alembic chain has no single head, so `alembic upgrade head` has no "
            f"resolvable target. Observed in the maintained chain: {detail or 'multiple heads'}. "
            f"Blocked by issue #{blocked.get('issue')} ({blocked.get('unit')}), which owns the "
            f"chain repair in modules/domain-apps/superplane/src/superplane-api/alembic/ — an "
            f"ADP-maintained directory since U22 (#5326), so the repair is an ordinary PR here "
            f"rather than a change to someone else's repository. Nothing was changed."
        )
    if schema.get("status") != "verified":
        raise Refusal(
            f"The lock records schema.status = {schema.get('status')!r}. A chain that has not "
            f"been verified against a real database must not be migrated from CI: the first "
            f"thing a bad head does is apply half of itself. Nothing was changed."
        )


def resolve_migration_image(lock: dict, image_name: str) -> str:
    """The digest of the image carrying the chain — or a refusal naming why there is none.

    The chain ships inside `superplane-api`, so that image IS the runner. The lock records it
    as pending with no digest (blocked by `source_access`), and this refusal is derived from
    the lock so that promotion unblocks the lane with no edit here.
    """
    pending = lock.get("pending_images") or {}
    if image_name in pending:
        entry = pending[image_name] or {}
        access = lock.get("source_access") or {}
        raise Refusal(
            f"The migration runner image {image_name!r} has no digest: the release lock records "
            f"it under `pending_images`, blocked by {entry.get('blocked_by')!r}. The Alembic "
            f"chain ships inside this image, so there is no artifact to run. "
            f"source_access.status = {access.get('status')!r}. This is an unavailable INPUT, "
            f"not a defect in this lane — resolving it is U2's, and the lane unblocks by data "
            f"once the lock promotes the image. Nothing was changed."
        )

    images = lock.get("images") or {}
    digest = images.get(image_name)
    if not digest:
        raise Refusal(
            f"The release lock neither pins nor records as pending an image named "
            f"{image_name!r}. Refusing to guess a migration runner: an improvised runner is a "
            f"second chain nobody reviewed."
        )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(digest)):
        raise Refusal(
            f"The lock's digest for {image_name!r} is {digest!r}, which is not a sha256 "
            f"digest. A migration must run a pinned artifact — nothing else can say afterwards "
            f"what was applied to the schema."
        )

    sources = lock.get("image_sources") or {}
    source = sources.get(image_name) or {}
    repository = source.get("ecr_repository") or source.get("repository")
    if not repository:
        raise Refusal(
            f"The lock pins a digest for {image_name!r} but records no repository under "
            f"`image_sources`, so the digest cannot be turned into a pullable reference."
        )
    registry = source.get("registry")
    prefix = f"{registry}/{repository}" if registry else repository
    return f"{prefix}@{digest}"


def check_no_local_chain(migrations_dir: Path) -> None:
    """A chain copied here is a second chain. The API component owns the only one.

    After U22 (#5326) the chain is ADP-maintained, which changes who owns it but not the
    single-copy rule — see CHAIN_FILE_RE for why the transfer makes this guard more load-bearing
    rather than redundant.
    """
    if not migrations_dir.is_dir():
        return
    offenders = sorted(
        entry.name
        for entry in migrations_dir.iterdir()
        if CHAIN_FILE_RE.match(entry.name)
    )
    if offenders:
        raise Refusal(
            f"{migrations_dir} contains what looks like an Alembic chain ({', '.join(offenders)}). "
            f"The chain belongs to the superplane-api component, at "
            f"modules/domain-apps/superplane/src/superplane-api/alembic/, and #5045 (U13) "
            f"repairs it THERE. A second copy diverges from it the first time either side "
            f"changes, and `alembic upgrade head` would then resolve a head the component does "
            f"not have. This directory holds the Job and its contract only."
        )


# ---------------------------------------------------------------------------
# Evidence. Presented by the operator, verified here — never assumed.
# ---------------------------------------------------------------------------


def check_target_evidence(evidence: dict) -> list[str]:
    """The migration target must be identified by something that was actually observed.

    Decision 2 (shared instance vs. separate) is unresolved, so this lane cannot discover
    what database it is pointed at. It requires the operator to present the identity and
    verifies the presented values are real observations rather than placeholders.
    """
    problems: list[str] = []
    target = evidence.get("target") or {}
    if not target:
        return ["no `target` block: the database being migrated was never identified"]

    for field in ("identifier", "endpoint", "database", "schema"):
        value = target.get(field)
        if not value or not str(value).strip():
            problems.append(f"target.{field} is missing or empty")
        elif PLACEHOLDER_RE.search(str(value)) or str(value).lower() in {
            "tbd",
            "unknown",
            "n/a",
            "none",
            "todo",
        }:
            problems.append(
                f"target.{field} is {value!r}, which is a placeholder rather than an observed "
                f"value"
            )

    observed_by = target.get("observed_by")
    if not observed_by or not str(observed_by).strip():
        problems.append(
            "target.observed_by is missing: it must name the command whose output established "
            "this identity (e.g. an `aws rds describe-db-instances` call), so the record is "
            "evidence rather than an assertion"
        )

    for field, value in target.items():
        if isinstance(value, str) and INLINE_CREDENTIAL_RE.search(value):
            problems.append(
                f"target.{field} contains an inline credential in a URI. The evidence record "
                f"is written to logs and a step summary; a password must never appear in it"
            )
    return problems


def check_backup_evidence(evidence: dict) -> list[str]:
    """A restorable backup must be evidenced, not warned about.

    The previous lane printed `::warning::` and continued. A warning does not stop a step, so
    the upgrade ran with no backup and the log looked as though a check had happened.
    """
    problems: list[str] = []
    backup = evidence.get("backup") or {}
    if not backup:
        return [
            "no `backup` block: this lane takes no backup and makes no durability claim, so it "
            "requires evidence that a restorable one exists before mutating a schema"
        ]

    for field in ("identifier", "created_at", "verified_by"):
        value = backup.get(field)
        if not value or not str(value).strip():
            problems.append(f"backup.{field} is missing or empty")
        elif PLACEHOLDER_RE.search(str(value)) or str(value).lower() in {
            "tbd",
            "unknown",
            "n/a",
            "none",
            "todo",
        }:
            problems.append(
                f"backup.{field} is {value!r}, a placeholder rather than a real value"
            )

    status = str(backup.get("status") or "").lower()
    if status != "available":
        problems.append(
            f"backup.status is {backup.get('status')!r}. Only 'available' is accepted: a "
            f"snapshot that is still creating is not a snapshot that can be restored from"
        )

    # The backup must belong to the database being migrated. A snapshot of a different
    # instance satisfies a checkbox and nothing else.
    target_identifier = ((evidence.get("target") or {}).get("identifier") or "").strip()
    source = str(backup.get("source_identifier") or "").strip()
    if not source:
        problems.append(
            "backup.source_identifier is missing, so nothing establishes that the snapshot is "
            "of the database this migration will change"
        )
    elif target_identifier and source != target_identifier:
        problems.append(
            f"backup.source_identifier is {source!r} but the migration target is "
            f"{target_identifier!r}. A snapshot of a different database is not a backup of this "
            f"one"
        )
    return problems


# ---------------------------------------------------------------------------
# The Job's declared boundary.
# ---------------------------------------------------------------------------


def _parse_search_path(pgoptions: str) -> list[str]:
    match = re.search(r"-c\s+search_path\s*=\s*([^\s]+)", pgoptions)
    if not match:
        return []
    return [
        part.strip().strip('"') for part in match.group(1).split(",") if part.strip()
    ]


def check_job_boundary(
    job: dict, *, schema: str, namespace: str, pinned_digest: str
) -> list[str]:
    """Verify the rendered Job declares the session boundary that replaces the text scan."""
    problems: list[str] = []

    if job.get("kind") != "Job":
        return [f"the rendered migration manifest is a {job.get('kind')!r}, not a Job"]

    metadata = job.get("metadata") or {}
    if metadata.get("namespace") != namespace:
        problems.append(
            f"the Job targets namespace {metadata.get('namespace')!r} but the domain's "
            f"namespace is {namespace!r}"
        )

    spec = job.get("spec") or {}
    if spec.get("backoffLimit") != 0:
        problems.append(
            f"backoffLimit is {spec.get('backoffLimit')!r}, not 0. A retried migration applies "
            f"its second half to a schema its first half already changed"
        )

    pod = ((spec.get("template") or {}).get("spec")) or {}
    if pod.get("restartPolicy") != "Never":
        problems.append(f"restartPolicy is {pod.get('restartPolicy')!r}, not Never")

    service_account = pod.get("serviceAccountName")
    if not service_account or service_account == "default":
        problems.append(
            f"serviceAccountName is {service_account!r}. The migration must connect as the "
            f"domain's own identity, whose IRSA role can read this module's secret and no other"
        )

    containers = pod.get("containers") or []
    if len(containers) != 1:
        problems.append(f"expected exactly one container, found {len(containers)}")
        return problems
    container = containers[0]

    image = container.get("image") or ""
    if image != pinned_digest:
        problems.append(
            f"the Job runs {image!r} but the lock pins the migration runner at "
            f"{pinned_digest!r}. Nothing could say afterwards what was applied to the schema"
        )

    command = container.get("command") or []
    if command and command[0] in {"sh", "bash", "/bin/sh", "/bin/bash"}:
        problems.append(
            f"the entrypoint is {command[0]!r}, so the arguments are shell source rather than "
            f"an argument vector; a substituted value would become executable text"
        )

    env = {entry.get("name"): entry for entry in container.get("env") or []}
    missing = REQUIRED_ENV - set(env)
    if missing:
        problems.append(
            f"the Job declares no {', '.join(sorted(missing))}. Without them the session has no "
            f"schema boundary and the only thing standing between a domain migration and a "
            f"gateway table would be a text scan, which does not work"
        )

    connection = env.get("SUPERPLANE_DB_CONNECTION_URI") or {}
    if "value" in connection:
        problems.append(
            "SUPERPLANE_DB_CONNECTION_URI carries a literal value. The connection must be a "
            "secretKeyRef: an applied manifest containing a password has to be rotated "
            "everywhere once noticed (upstream's own db-migrate-job.yaml does this)"
        )
    else:
        ref = (connection.get("valueFrom") or {}).get("secretKeyRef") or {}
        if not ref:
            problems.append(
                "SUPERPLANE_DB_CONNECTION_URI is not read from a secretKeyRef"
            )
        elif ref.get("optional") is not False:
            problems.append(
                "SUPERPLANE_DB_CONNECTION_URI's secretKeyRef is not `optional: false`. With an "
                "absent secret the pod would start with an empty connection string, and a "
                "migration that connected to nothing must not be able to report success"
            )

    declared_schema = (env.get("SUPERPLANE_DB_SCHEMA") or {}).get("value")
    if declared_schema != schema:
        problems.append(
            f"SUPERPLANE_DB_SCHEMA is {declared_schema!r} but the domain schema is {schema!r}"
        )

    search_path = _parse_search_path((env.get("PGOPTIONS") or {}).get("value") or "")
    if not search_path:
        problems.append(
            "PGOPTIONS declares no search_path. This is THE mechanism that replaces the SQL "
            'text scan: without it an unqualified op.drop_table("users") resolves to the '
            "gateway's table"
        )
    else:
        forbidden = sorted(set(search_path) & FORBIDDEN_SEARCH_PATH_SCHEMAS)
        if forbidden:
            problems.append(
                f"search_path includes {', '.join(forbidden)}. With `public` reachable, an "
                f"unqualified DDL statement resolves to another module's table — which is "
                f"exactly the case the text scan could not catch"
            )
        if search_path != [schema]:
            problems.append(
                f"search_path is {search_path}, which is not exactly [{schema!r}]. Every extra "
                f"entry is a namespace an unqualified statement can reach"
            )

    version_table = (env.get("ALEMBIC_VERSION_TABLE") or {}).get("value")
    if version_table == DEFAULT_VERSION_TABLE:
        problems.append(
            f"ALEMBIC_VERSION_TABLE is the default {DEFAULT_VERSION_TABLE!r}, shared with the "
            f"gateway's chain. Two chains on one version table each treat the other's "
            f"revisions as unknown and attempt to re-apply their own"
        )
    version_schema = (env.get("ALEMBIC_VERSION_TABLE_SCHEMA") or {}).get("value")
    if version_schema != schema:
        problems.append(
            f"ALEMBIC_VERSION_TABLE_SCHEMA is {version_schema!r}, not the domain schema "
            f"{schema!r}; the bookkeeping table would live outside the boundary it records"
        )

    for entry in container.get("env") or []:
        value = entry.get("value")
        if isinstance(value, str) and INLINE_CREDENTIAL_RE.search(value):
            problems.append(
                f"env {entry.get('name')} contains an inline credential in a URI"
            )

    resources = container.get("resources") or {}
    for field in ("requests", "limits"):
        for dimension in ("cpu", "memory"):
            if dimension not in (resources.get(field) or {}):
                problems.append(
                    f"the container declares no resources.{field}.{dimension}"
                )

    return problems


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def _load_yaml(path: Path, what: str) -> dict:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise Refusal(f"could not read the {what} at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise Refusal(f"the {what} at {path} did not parse as a mapping")
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock-file", required=True, type=Path)
    parser.add_argument("--migrations-dir", required=True, type=Path)
    parser.add_argument(
        "--image-name",
        default="superplane-api",
        help="The lock entry for the image carrying the Alembic chain.",
    )
    parser.add_argument(
        "--job-file",
        type=Path,
        help=(
            "A RENDERED Job manifest to verify. Omit to check only input availability and the "
            "chain-ownership rule — which is what the lane can do while the runner image is "
            "unavailable."
        ),
    )
    parser.add_argument("--schema", default="", help="The domain's database schema.")
    parser.add_argument(
        "--namespace", default="", help="The domain's Kubernetes namespace."
    )
    parser.add_argument(
        "--evidence-file",
        type=Path,
        help=(
            "Operator-presented JSON evidence of the migration target and a restorable "
            "backup. Required before any mutation; absence is a refusal, never a warning."
        ),
    )
    parser.add_argument(
        "--require-evidence",
        action="store_true",
        help="Set for a mutating run. Without it this is a read-only contract check.",
    )
    parser.add_argument(
        "--emit-image",
        type=Path,
        help=(
            "Write the resolved pinned runner reference to this file. Written ONLY on success, "
            "so a caller cannot proceed with a reference this script refused. The workflow "
            "reads the file rather than scraping stdout: a message is for a person, and "
            "parsing one is a contract nobody declared."
        ),
    )
    args = parser.parse_args(argv)

    try:
        lock = _load_yaml(args.lock_file, "release lock")

        check_no_local_chain(args.migrations_dir)
        check_chain_is_resolvable(lock)
        pinned = resolve_migration_image(lock, args.image_name)
        print(f"Migration runner pinned by the lock: {pinned}")

        problems: list[str] = []

        if args.require_evidence:
            if not args.evidence_file:
                raise Refusal(
                    "a mutating run requires --evidence-file. This lane takes no backup and "
                    "makes no durability claim about the target (Decision 2 is unresolved), so "
                    "it refuses to mutate a schema without verified evidence of the target and "
                    "of a restorable backup. This is a refusal, not a warning: the previous "
                    "version printed `::warning::` and continued to the upgrade."
                )
            try:
                evidence = json.loads(args.evidence_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise Refusal(f"could not read the evidence file: {exc}") from exc
            if not isinstance(evidence, dict):
                raise Refusal("the evidence file did not parse as a JSON object")
            problems += check_target_evidence(evidence)
            problems += check_backup_evidence(evidence)

        if args.job_file:
            if not args.schema or not args.namespace:
                raise Refusal(
                    "--schema and --namespace are required with --job-file: without them the "
                    "boundary checks would compare the Job against nothing and pass vacuously"
                )
            job = _load_yaml(args.job_file, "rendered migration Job")
            residual = PLACEHOLDER_RE.search(args.job_file.read_text(encoding="utf-8"))
            if residual:
                raise Refusal(
                    f"the rendered Job still contains {residual.group(0)}; it would be applied "
                    f"literally"
                )
            problems += check_job_boundary(
                job, schema=args.schema, namespace=args.namespace, pinned_digest=pinned
            )

        if problems:
            print("::error::The migration contract does not hold:")
            for problem in problems:
                print(f"  - {problem}")
            print()
            print(
                "Nothing was changed. NOTE: the database boundary is the session's search_path, "
                "the domain-scoped version table and the domain's own credential — NOT a scan "
                "of the migration SQL. A text scan cannot enumerate the ways to name a table: "
                'op.drop_table("users") emits none of the words such a scan looks for.'
            )
            return 1

    except Refusal as exc:
        print(f"::error::{exc}")
        return 1

    if args.emit_image:
        # Only here — after every refusal has been passed. A file written earlier would let a
        # caller that ignored the exit code proceed with a reference this script rejected.
        args.emit_image.write_text(pinned, encoding="utf-8")

    print("Migration contract holds.")
    print(
        "NOTE: no isolation, backup, retention or restore property of the target database is "
        "claimed. Decision 2 (shared vs. separate instance) is unresolved, so this module does "
        "not know what it connects to. `search_path` constrains NAME RESOLUTION, not PRIVILEGE: "
        "a schema-qualified statement still reaches another schema if the connecting role has "
        "rights there, and that grant is made on the database, not here."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
