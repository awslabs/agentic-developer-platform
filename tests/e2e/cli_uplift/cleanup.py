"""Durable cleanup manifest and the independent TTL recovery sweep.

The issue is explicit that `always()` is not sufficient, and it is right: a
cancelled workflow, a lost runner or an OOM-killed step all skip the cleanup
step entirely. Nothing that lives only inside the run can guarantee teardown.

So there are two independent mechanisms:

1. A durable private manifest (0600 in a 0700 directory) written BEFORE each
   mutating call, so an interrupted run still leaves an exact record of what it
   created. `sweep()` reads it and deletes by recorded ID.
2. A recovery sweep keyed on tags and instance age, which needs no manifest at
   all — it finds this evaluation's resources by tag and terminates anything
   past its TTL. This is what catches the run whose runner vanished before it
   could even write the manifest entry.

Two rules run through everything here:

- Delete only what this run created. Every deletion is gated on the ownership
  tag carrying our own prefix, so a concurrent evaluation's instances and any
  shared resource are untouchable. The issue's "never purge shared queues" is
  enforced structurally: `purge_queue` is not a capability this module has.
- Reused resources are preserved. A pre-existing GitHub App or an imported role
  we did not create is recorded as `reused` and skipped, because deleting a
  fixture someone lent us is worse than leaking one we made.
- Every resource knows which ACCOUNT and REGION it lives in, and is deleted
  through a session scoped to that account (R7). Previously every deleter closed
  over the platform session, so the destination account's CloudFormation stack
  was deleted with platform credentials: an AccessDenied that the sweep reported
  as a failure at best, and at worst a stack silently left standing while the run
  reported clean. Location is part of a resource's identity here, not context the
  caller is trusted to remember.
"""

from __future__ import annotations

import calendar
import fcntl
import json
import os
import time
from pathlib import Path

# The tag every run-created resource carries. Value is always the run prefix, so
# a tag match alone is never sufficient — the VALUE must be ours.
OWNER_TAG = "adp:cli-uplift-eval"

# Kept recognizable for manifests from interrupted E18 attempts. Recovery of
# these resources is currently refused: the producer cannot durably publish the
# CLI's operation identity before its first POST, and cleanup has no verified
# ordinary-principal recovery session. Registering a kind does not authorize a
# best-guess delete with the run's administrator session.
SUPERPLANE_KINDS = (
    "superplane_deployment",
    "superplane_workspace",
    "superplane_provider",
    "adp_vault_credential",
    "superplane_account",
)
SUPERPLANE_RECOVERY_BLOCKER = (
    "E18 durable recovery is not implemented: CLI operation receipts must be "
    "published before mutation and recovered by exact resource identity under "
    "the original ordinary or administrator principal. Superplane mutations "
    "remain disabled until the producer and scoped recovery deleters exist."
)

# Resource kinds the sweep knows how to delete, in dependency order: instances
# first (they hold the ENI), then the things they referenced.
ORDER = (
    "ec2_instance",
    "iam_instance_profile",
    "iam_role",
    "security_group",
    "s3_object",
    "secret",
    "cognito_user",
    "adp_connection",
    # A destination registered through `adp admin bedrock connect`. Recorded by
    # the E06 journey; without it here, `record()` raises on an unknown kind and
    # `sweep()` treats it as a leak — a successful E06 would have failed cleanup.
    "bedrock_destination",
    *SUPERPLANE_KINDS,
    # The ADP account the run registers for its own E02 login, so the user-scoped
    # routes later cases exercise have a `users` row to resolve to.
    #
    # LAST of the API-deleted kinds, deliberately. Deleting this row is deleting
    # the very account the deleters above authenticate as, and the connection and
    # destination deleters call user-scoped endpoints that resolve a Cognito
    # subject to it — remove it first and they answer 404 `user_not_found`, so a
    # run would report a leak for resources it could no longer even see. It is
    # after `cognito_user` for the same reason in reverse: the product's delete
    # cascades to the login, so by the time this runs that login is already gone
    # and the cascade is a best-effort no-op. The bearer token stays valid either
    # way — a JWT is checked by signature and expiry, not by the login still
    # existing.
    #
    # It CANNOT be moved before `ec2_instance`, and does not need to be: the
    # instance is terminated first because it holds the ENI, and the token these
    # API deleters need lives in a vault on it. `live._deleters`' `build` therefore
    # reads that token eagerly, before `sweep()` runs anything — see the comment
    # there. Order within the sweep is a dependency statement about the RESOURCES,
    # not about where credentials come from.
    "adp_user",
    "cloudformation_stack",
    "github_app",
)

# Kinds that can exist in EITHER account, so their location must be recorded
# explicitly. `record()` refuses them without one rather than defaulting to the
# platform account — defaulting is exactly how the destination stack came to be
# deleted with platform credentials. Everything else defaults to the platform
# account, which is where the harness's own resources are created.
LOCATION_REQUIRED = frozenset({"cloudformation_stack", "iam_role", "security_group"})

PENDING = "pending"
DELETED = "deleted"
REUSED = "reused"
FAILED = "failed"
SKIPPED_NOT_OWNED = "skipped_not_owned"


class Manifest:
    """Append-only record of run-created resources, safe across processes.

    Written before the mutating call, never after: a resource created but not
    recorded is a leak nothing can find, whereas a resource recorded but never
    created is a harmless no-op delete.

    R6: local durability is not enough. The manifest lives in the runner's /tmp,
    which dies with the runner, and the run used to push it to the durable store
    only once before the stages started and once in an outer `finally` — so a
    cancellation, an OOM kill or a lost runner mid-journey lost every intent
    recorded in between, and those resources (IAM roles, stacks, secrets, Cognito
    users) have no age-based sweep to fall back on. `on_change` is called after
    each recorded intent and each status change so the durable copy keeps up with
    the local one, BEFORE the mutation those intents describe.

    `on_change(document, *, critical)` is told which kind of change it is:

    - `critical=True` for a newly recorded intent. The caller's very next line
      creates the resource, so if this push fails the resource would exist with
      no durable record anywhere. The hook is expected to RAISE, which stops the
      run before the mutation.
    - `critical=False` for a status change (a completed deletion). Here the push
      failing must NOT raise: the sweep is the teardown, and aborting it because
      the store is unreachable would strand the very resources it was deleting.
    """

    def __init__(self, path, prefix, *, on_change=None):
        self.path = Path(path)
        self.prefix = prefix
        self.on_change = on_change
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if not self.path.exists():
            self._write(
                {"prefix": prefix, "created_at": int(time.time()), "resources": []}
            )

    def _write(self, document):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        temporary.chmod(0o600)
        # Atomic replace: a crash mid-write cannot truncate the manifest.
        temporary.replace(self.path)
        self.path.chmod(0o600)

    def read(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"prefix": self.prefix, "resources": []}

    def _mutate(self, change, *, critical):
        """Apply `change` under an exclusive lock so parallel journeys interleave.

        The durable push happens after the lock is released but before this
        returns, so the caller's next line — the mutating AWS or gateway call —
        cannot run until the intent is durable. Inside the lock it would serialize
        an S3 round trip against every parallel journey; after the return it would
        be too late to be a precondition.
        """
        with open(self.path, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                try:
                    document = json.loads(handle.read())
                except ValueError:
                    document = {"prefix": self.prefix, "resources": []}
                result = change(document)
                self._write(document)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        if self.on_change is not None:
            self.on_change(document, critical=critical)
        return result

    def record(
        self, kind, identifier, *, reused=False, detail=None, account=None, region=None
    ):
        """Record intent BEFORE the mutating call. Idempotent per (kind, id).

        `account`/`region` are the resource's own location, carried through to the
        deleter so it can act with credentials for the right account (R7). Kinds in
        `LOCATION_REQUIRED` may exist in either account and must say which; the
        rest default to the platform session the harness runs under.
        """
        if kind not in ORDER:
            raise ValueError(f"Unknown resource kind: {kind}")
        if not identifier:
            raise ValueError(f"A {kind} must be recorded with a concrete identifier")
        if kind in LOCATION_REQUIRED and not account:
            raise ValueError(
                f"A {kind} must be recorded with the account it lives in; "
                "deleting it with the wrong account's credentials would either "
                "fail or leave it standing while the run reported clean"
            )

        def change(document):
            for entry in document["resources"]:
                if entry["kind"] == kind and entry["id"] == identifier:
                    return entry
            entry = {
                "kind": kind,
                "id": identifier,
                "prefix": self.prefix,
                "status": REUSED if reused else PENDING,
                "recorded_at": int(time.time()),
                "detail": detail or {},
            }
            if account:
                entry["account"] = str(account)
            if region:
                entry["region"] = str(region)
            document["resources"].append(entry)
            return entry

        # Critical: the caller creates the resource on its next line. If the
        # durable push fails, the intent must not be treated as recorded.
        return self._mutate(change, critical=True)

    def mark(self, kind, identifier, status, *, error=None):
        def change(document):
            for entry in document["resources"]:
                if entry["kind"] == kind and entry["id"] == identifier:
                    # A reused resource is never re-marked as deleted; that would
                    # misreport us as having removed someone else's fixture.
                    if entry["status"] == REUSED and status == DELETED:
                        return entry
                    entry["status"] = status
                    if error:
                        entry["error"] = error
                    return entry
            return None

        # Not critical: this records that a resource is already gone (or failed to
        # go). A store outage here must not abort the sweep, or an unreachable
        # bucket would strand every resource still to be deleted.
        return self._mutate(change, critical=False)

    def outstanding(self):
        """Resources still needing deletion, in dependency order."""
        resources = self.read().get("resources", [])
        return sorted(
            (entry for entry in resources if entry.get("status") in (PENDING, FAILED)),
            key=lambda entry: ORDER.index(entry["kind"])
            if entry["kind"] in ORDER
            else len(ORDER),
        )

    def summary(self):
        counts = {}
        for entry in self.read().get("resources", []):
            counts[entry.get("status", PENDING)] = (
                counts.get(entry.get("status", PENDING), 0) + 1
            )
        return counts


def owned(entry, prefix):
    """Ownership gate. A resource is deletable only if this run created it."""
    return (
        bool(prefix) and entry.get("prefix") == prefix and entry.get("status") != REUSED
    )


def location(entry):
    """Where a recorded resource lives: (account, region), either possibly None.

    None means "the harness's own platform session", which is the correct default
    for the resources the harness creates itself. It is never a default for a kind
    that could be in either account — `record()` rejects those without a location.
    """
    return (entry.get("account"), entry.get("region"))


def sweep(manifest, deleters, *, prefix=None):
    """Delete every outstanding run-owned resource. Returns (ok, results).

    `deleters` maps a kind to `callable(identifier, *, account, region)`. Missing
    kinds are a hard failure rather than a silent skip: an unhandled kind means a
    leak that reports as clean, which is exactly the outcome the issue forbids.

    The location is passed to the deleter rather than baked into it (R7), so a
    stack in the destination account is deleted with destination credentials.
    A deleter that does not accept the keywords is called positionally, which
    keeps a simple test double one lambda rather than a signature exercise.

    A deleter that raises is recorded as failed and the sweep continues — one
    stuck resource must not strand the others — but `ok` is False, and a False
    here makes full acceptance non-successful via `cases.accept(cleanup_ok=...)`.
    """
    prefix = prefix or manifest.prefix
    results, ok = [], True
    for entry in manifest.outstanding():
        kind, identifier = entry["kind"], entry["id"]
        if not owned(entry, prefix):
            manifest.mark(kind, identifier, SKIPPED_NOT_OWNED)
            results.append(
                {"kind": kind, "id": identifier, "status": SKIPPED_NOT_OWNED}
            )
            continue
        deleter = deleters.get(kind)
        if deleter is None:
            ok = False
            manifest.mark(kind, identifier, FAILED, error="no deleter registered")
            results.append(
                {
                    "kind": kind,
                    "id": identifier,
                    "status": FAILED,
                    "error": "no deleter registered",
                }
            )
            continue
        account, region = location(entry)
        try:
            try:
                deleter(identifier, account=account, region=region)
            except TypeError:
                # A deleter that takes the identifier alone. Only acceptable for a
                # resource with no recorded location; otherwise the location would
                # be silently discarded, which is the R7 defect itself.
                if account:
                    raise
                deleter(identifier)
        except Exception as exc:
            ok = False
            # Type name only: a provider message can quote a token or an ARN with
            # an ExternalId in it.
            manifest.mark(kind, identifier, FAILED, error=type(exc).__name__)
            results.append(
                {
                    "kind": kind,
                    "id": identifier,
                    "status": FAILED,
                    "error": type(exc).__name__,
                    "account": account,
                }
            )
        else:
            manifest.mark(kind, identifier, DELETED)
            results.append(
                {
                    "kind": kind,
                    "id": identifier,
                    "status": DELETED,
                    "account": account,
                }
            )
    return ok, results


def launch_epoch(stamp):
    """EC2's `LaunchTime` as a UTC epoch, or None if it cannot be parsed.

    EC2 reports `LaunchTime` in UTC (`2026-09-16T06:00:00.000Z`). The obvious
    `time.mktime(time.strptime(...))` is wrong for it: `mktime` interprets the
    struct as LOCAL time, so on a runner outside UTC every launch stamp is
    shifted by the offset. Skewed one way that makes a just-launched instance
    look hours old; skewed the other it reports a NEGATIVE age, which no
    positive TTL can ever expire — the sweep then walks past a running instance
    and reports the account clean. `calendar.timegm` is the UTC-correct
    inverse of `strptime` and is what keeps age comparisons runner-independent.

    Returning None on an unparseable stamp preserves the existing rule in
    `expired_instances`: without a provable age we leave the instance to the
    manifest path rather than terminate on a guess.
    """
    if stamp is None:
        return None
    if not isinstance(stamp, str):
        # Already an epoch (the in-run path and most tests pass floats).
        return float(stamp)
    try:
        return float(calendar.timegm(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, TypeError):
        return None


def expired_instances(instances, *, now, ttl_minutes, prefix=None):
    """Instances this evaluation owns that have outlived their TTL.

    This is the manifest-independent half of the guarantee. It matches on the
    ownership tag and on age, so it recovers a run that died before recording
    anything. `prefix` scopes it to one run; without it, every expired
    evaluation instance in the account is a candidate — which is what the
    scheduled recovery sweep wants, and what a single run must never do.

    Terminated and shutting-down instances are ignored so the sweep is
    idempotent and its report does not inflate with repeat kills.
    """
    cutoff = now - ttl_minutes * 60
    expired = []
    for instance in instances:
        state = ((instance.get("State") or {}).get("Name") or "").lower()
        if state in ("terminated", "shutting-down"):
            continue
        tags = {tag.get("Key"): tag.get("Value") for tag in instance.get("Tags") or []}
        owner = tags.get(OWNER_TAG)
        if not owner:
            continue
        if prefix and owner != prefix:
            continue
        launched = launch_epoch(instance.get("LaunchTime"))
        if launched is None:
            # No launch time means we cannot prove it is expired. Leave it to the
            # manifest path rather than terminate on a guess.
            continue
        if launched <= cutoff:
            expired.append(
                {
                    "id": instance.get("InstanceId"),
                    "prefix": owner,
                    "age_minutes": int((now - launched) // 60),
                }
            )
    return [item for item in expired if item["id"]]


def owned_by_run(instances, prefix):
    """Every live instance tagged for ONE run, regardless of age.

    The age-based sweep cannot recover an early cancellation. A run cancelled
    ten minutes in has an instance that is validly tagged but nowhere near its
    TTL, so `expired_instances()` correctly refuses it and the resource then
    bills until the TTL elapses — hours later, and only if some later run
    happens to sweep. Once we know a specific run is over, its age is
    irrelevant: the ownership tag alone is sufficient authority.

    `prefix` is REQUIRED and must be a concrete evaluation ID. That is the whole
    safety argument: this deletes without an age check, so it must never be able
    to select another run's resources. An empty or missing prefix raises rather
    than matching everything.
    """
    if not prefix:
        raise ValueError(
            "owned_by_run requires a specific evaluation ID; refusing to select "
            "every tagged instance without an age check"
        )
    found = []
    for instance in instances:
        state = ((instance.get("State") or {}).get("Name") or "").lower()
        if state in ("terminated", "shutting-down"):
            continue
        tags = {tag.get("Key"): tag.get("Value") for tag in instance.get("Tags") or []}
        if tags.get(OWNER_TAG) != prefix:
            continue
        if instance.get("InstanceId"):
            found.append(
                {
                    "id": instance["InstanceId"],
                    "prefix": prefix,
                    "reason": "run_ended",
                }
            )
    return found


def recoverable_instances(instances, *, now, ttl_minutes, prefix=None):
    """What a recovery sweep may terminate: this run's, plus anything expired.

    Two authorities, unioned, because they cover different failures:

    - `prefix` given (this run ended, however it ended): every instance tagged
      with that exact ID, at any age. Covers cancellation seconds after launch.
    - age past TTL, any prefix: covers a run that died before it could record
      anything and whose ID we no longer know.

    Deduplicated by instance ID so a resource satisfying both is reported once.
    """
    candidates = {}
    if prefix:
        for item in owned_by_run(instances, prefix):
            candidates[item["id"]] = item
    for item in expired_instances(instances, now=now, ttl_minutes=ttl_minutes):
        candidates.setdefault(item["id"], {**item, "reason": "ttl_expired"})
    return [candidates[key] for key in sorted(candidates)]


def recovery_report(expired, observed, *, ttl_minutes):
    """What the recovery sweep did, for the workflow summary.

    `observed` maps instance ID to the state a `describe-instances` RE-READ
    reported AFTER the terminate call — not the set of calls that returned zero.
    That distinction is the whole point of this signature. `terminate-instances`
    exits 0 for an instance held by `DisableApiTermination`, or one stuck in
    `stopping` behind a lifecycle hook: the API accepted the request, the
    instance keeps running, and the bill keeps growing. Reporting `clean` from
    exit codes therefore announces success for exactly the failure this sweep
    exists to catch, and the operator stops looking.

    The in-run path already gets this right (`live.terminate` polls until state
    is `terminated` and raises otherwise). The sweep is the path that runs when
    the manifest is GONE, so it is the one that most needs the same proof.

    Only `terminated` counts. `shutting-down` is deliberately outstanding: it is
    a promising direction, not a finished teardown, and a sweep that treats it
    as done can still exit while an instance is billable.
    """
    wanted = {item["id"] for item in expired}
    confirmed = {
        instance_id
        for instance_id, state in (observed or {}).items()
        if str(state or "").lower() == "terminated" and instance_id in wanted
    }
    return {
        "ttl_minutes": ttl_minutes,
        "expired": [item["id"] for item in expired],
        "terminated": sorted(confirmed),
        "observed": {key: (observed or {})[key] for key in sorted(observed or {})},
        "outstanding": sorted(wanted - confirmed),
        "clean": wanted == confirmed,
    }
