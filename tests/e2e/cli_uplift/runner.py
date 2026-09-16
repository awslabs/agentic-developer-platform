"""Orchestrator: state, attempts, stage sequencing and the acceptance verdict.

Modes are `start`, `resume`, `status` and `cleanup`. The evaluation ID is
immutable across attempts — that is what lets a resumed run be reported as the
same evaluation rather than a fresh one that happens to look similar — and each
attempt gets its own ID so the transcript shows which attempt produced which
result.

Resume preserves prior results. It replays nothing that already passed, and it
does NOT inherit a prior `failed`: a failure has to be re-proven or re-fixed,
because silently carrying one forward would let a run go green on stale evidence
while carrying a failure forward would make a fixed case un-passable. Blocked and
not-run cases are retried, since a fixture may have appeared since.

Everything that touches AWS, the gateway or an instance arrives through the
`stages` mapping, so the whole orchestration — including cancellation and the
fault injections — is exercised offline with sockets disabled.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

from . import (
    bundle,
    cases,
    cleanup,
    config,
    ports,
    preflight,
    release,
    report,
    stages as stages_module,
    statestore,
)

STATE_DIR = ".adp-eval"
STATE_FILE = "state.json"
MANIFEST_FILE = "manifest.json"

# Stage order. Preflight is first because it is the only stage that can prove the
# target before a mutation; cleanup is last but is ALSO driven independently by
# the recovery sweep, so a crash between the two still terminates the instance.
STAGES = (
    "preflight",
    "ec2",
    "install_auth",
    "providers",
    "journeys",
    "evidence",
    "cleanup",
)

# Faults the workflow may inject, bounded to exactly these. An arbitrary
# fault string is rejected so a dispatch cannot invent a destructive one.
FAULTS = (
    "none",
    "wrong_account",
    "missing_usage",
    "cleanup_failure",
    "expired_token",
    "instance_loss",
)


class RunnerError(RuntimeError):
    """The run cannot proceed. Raised before or between stages, never mid-mutation."""


# The harness's own error types. Every one is documented to carry no provider
# text, which is what makes it safe to publish their messages in the report while
# a boto3 or urllib message — which can quote a token, an ARN or a bucket policy —
# contributes only its type.
OWN_ERRORS = (
    RunnerError,
    stages_module.StageError,
    ports.PortError,
    preflight.PreflightError,
    config.ConfigError,
    bundle.BundleError,
    release.ReleaseError,
    statestore.StateStoreError,
)


EVALUATION_ID = re.compile(r"^adp-e2e-[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")


def new_evaluation_id(now, entropy):
    """`adp-e2e-<utc>-<6 hex>`; also the ownership tag value for every resource."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    return f"adp-e2e-{stamp}-{hashlib.sha256(str(entropy).encode()).hexdigest()[:6]}"


def check_evaluation_id(value):
    """Reject an operator-supplied ID that is not in the canonical form.

    This runs before any resource exists, which is the whole point. The ID is
    both the ownership tag value that cleanup filters on and a pattern-validated
    field in report.schema.json, so a malformed one would otherwise launch real
    instances and only fail at publish time -- losing the report for a run that
    had already spent money, and leaving resources tagged with something the
    recovery sweep's filter does not expect.
    """
    if not EVALUATION_ID.match(str(value)):
        raise RunnerError(
            f"Evaluation ID {value!r} is not of the form adp-e2e-YYYYMMDD-HHMMSS-<6 hex>; "
            "omit --evaluation-id to generate one"
        )
    return value


def fingerprint(cfg):
    """Config identity, so a resume cannot silently retarget the evaluation."""
    material = {
        key: cfg.get(key)
        for key in (
            "gateway_url",
            "region",
            "platform_account",
            "destination_account",
            "expected_revision",
        )
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


class State:
    """Private, locked run state. Never an artifact: it holds live credentials.

    The file carries disposable Cognito passwords and session tokens until
    cleanup, which is why it is 0600 in a 0700 directory, git-ignored, and never
    attached anywhere. `report.json` is the publishable view.
    """

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / STATE_FILE

    def exists(self):
        return self.path.exists()

    def read(self):
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text())
        except ValueError as exc:
            raise RunnerError(
                f"Run state is corrupt and cannot be resumed: {exc}"
            ) from None

    def write(self, document):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)
        return document

    def lock(self):
        """Refuse concurrent attempts on one state directory.

        Two runners sharing a state directory would interleave matrix writes and
        each could delete the other's instances mid-journey.
        """
        handle = open(self.dir / ".lock", "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RunnerError(
                "Another attempt is already running against this state directory"
            ) from None
        return handle


def initial_state(cfg, suites, evaluation_id, *, now, fault="none"):
    if fault not in FAULTS:
        raise RunnerError(
            f"Unknown fault injection {fault!r}; allowed: {', '.join(FAULTS)}"
        )
    matrix = cases.new_matrix(suites)
    return {
        "evaluation_id": evaluation_id,
        "attempt": 1,
        "attempt_id": f"{evaluation_id}-a1",
        "attempts": [
            {
                "id": f"{evaluation_id}-a1",
                "started_at": int(now),
                "ended_at": None,
                "status": "running",
            }
        ],
        "suites": list(suites),
        "fault": fault,
        "config_fingerprint": fingerprint(cfg),
        "expected_revision": cfg["expected_revision"],
        "harness_commit": config.HARNESS_COMMIT,
        "started_at": int(now),
        "deadline": int(now) + cfg["max_run_minutes"] * 60,
        "stages": {name: "pending" for name in STAGES},
        "matrix": matrix,
        "cleanup_ok": False,
        "correlation": {},
        "preflight": {},
        "transcript": [],
    }


def resumable(document, cfg, suites):
    """Validate that this state may be resumed against this config.

    A resume that quietly retargets is the worst outcome available here: it would
    report acceptance of revision B using results collected against revision A.
    """
    if document.get("config_fingerprint") != fingerprint(cfg):
        raise RunnerError(
            "Run state was created against a different target or revision; start a new evaluation instead"
        )
    if list(document.get("suites") or []) != list(suites):
        raise RunnerError(
            f"Run state covers suites {document.get('suites')}, not {list(suites)}; start a new evaluation instead"
        )
    return True


def next_attempt(document, *, now):
    """Open a new attempt, preserving passed results and retrying the rest."""
    attempt = int(document.get("attempt") or 1) + 1
    attempt_id = f"{document['evaluation_id']}-a{attempt}"
    document["attempt"], document["attempt_id"] = attempt, attempt_id
    document.setdefault("attempts", []).append(
        {
            "id": attempt_id,
            "started_at": int(now),
            "ended_at": None,
            "status": "running",
        }
    )
    # A prior failure must be re-proven; a prior pass stands.
    for case_id, entry in document["matrix"].items():
        if entry["status"] == cases.FAILED:
            document["matrix"][case_id] = {
                "status": cases.NOT_RUN,
                "detail": {"retried_from": "failed", "attempt": attempt},
            }
    # Every stage re-runs. What resume preserves is RESULTS, not stage progress:
    # a stage can complete while leaving one of its cases failed, so skipping
    # "already complete" stages would strip the only thing able to re-prove that
    # case and the attempt would report it as never run. Stages consult the
    # matrix and skip cases that already passed. Re-running preflight is also
    # correct — an attempt minutes or hours later must re-prove the deployed
    # revision rather than trust the previous attempt's reading of it.
    for name in document["stages"]:
        document["stages"][name] = "pending"
    # Cleanup is re-proven every attempt; a prior attempt's success says nothing
    # about the resources this attempt creates.
    document["cleanup_ok"] = False
    document["deadline"] = (
        int(now)
        + max(1, (document.get("deadline", 0) - document.get("started_at", 0)) // 60)
        * 60
    )
    return document


def close_attempt(document, status, *, now):
    for attempt in reversed(document.get("attempts") or []):
        if attempt["id"] == document.get("attempt_id"):
            attempt["ended_at"], attempt["status"] = int(now), status
            break
    return document


class Evaluation:
    """Drives the stage sequence and produces the verdict.

    `stages` maps a stage name to `callable(context) -> None`, mutating the
    matrix through `cases.record`. Anything that raises marks its stage failed
    and stops forward progress — but cleanup still runs, and the recovery sweep
    is a second, independent guarantee behind it.
    """

    def __init__(self, cfg, state, stages, *, clock=time.time, on_manifest_change=None):
        self.config = cfg
        self.state = state
        self.stages = stages
        self.clock = clock
        # R6: every recorded intent is pushed to the durable store before the
        # mutation it describes, rather than only at two points in the run.
        self.manifest = cleanup.Manifest(
            state.dir / MANIFEST_FILE, "", on_change=on_manifest_change
        )

    def context(self, document):
        self.manifest.prefix = document["evaluation_id"]
        return {
            "config": self.config,
            "document": document,
            "matrix": document["matrix"],
            "manifest": self.manifest,
            "prefix": document["evaluation_id"],
            "evaluation_id": document["evaluation_id"],
            "attempt_id": document["attempt_id"],
            "fault": document.get("fault", "none"),
            "record": lambda case_id, status, detail=None: cases.record(
                document["matrix"], case_id, status, detail
            ),
            "transcript": document["transcript"],
            "correlation": document["correlation"],
            "preflight": document["preflight"],
        }

    def run(self, document):
        ctx = self.context(document)
        failure = None
        for name in STAGES:
            if name == "cleanup":
                continue
            if document["stages"].get(name) == "complete":
                continue
            if self.clock() > document["deadline"]:
                document["stages"][name] = "timed_out"
                failure = failure or RunnerError(
                    f"Run exceeded max_run_minutes before {name}"
                )
                break
            stage = self.stages.get(name)
            if stage is None:
                # A missing stage is a failure, not a skip. Skipping was how a run
                # with no implementations at all reported fifteen `not_run` rows
                # and six tidy "skipped" stages, which reads as a healthy harness
                # that simply had nothing to do.
                document["stages"][name] = "missing"
                document.setdefault("errors", []).append(
                    {
                        "stage": name,
                        "type": "MissingStage",
                        "message": f"No implementation registered for required stage {name!r}",
                    }
                )
                failure = failure or RunnerError(
                    f"Required stage {name!r} has no implementation"
                )
                break
            document["stages"][name] = "running"
            self.state.write(document)
            try:
                stage(ctx)
            except Exception as exc:
                document["stages"][name] = "failed"
                # Message only for the harness's OWN error types, every one of
                # which is written to carry no provider text. A foreign exception
                # contributes its type alone, since boto3 and urllib messages can
                # quote a token, an ARN or a bucket policy.
                #
                # The list matters: without PortError and StageError here, the
                # commonest real failures — a missing binding, an unreachable
                # gateway, a bundle that would not install — reported as a bare
                # type name, and an operator had nothing to act on.
                document.setdefault("errors", []).append(
                    {
                        "stage": name,
                        "type": type(exc).__name__,
                        "message": str(exc) if isinstance(exc, OWN_ERRORS) else None,
                    }
                )
                failure = failure or exc
                break
            document["stages"][name] = "complete"
            self.state.write(document)

        # Cleanup runs whatever happened above. It is inside the same process, so
        # it does not cover cancellation — the TTL sweep does.
        document["cleanup_ok"] = self.cleanup(ctx, document)
        # Stages are part of the verdict. `failure` above is a local variable that
        # main() discards, so grading on the matrix alone let a resumed attempt
        # publish acceptance while its own preflight had rejected the target.
        # Deriving the veto from the persisted stage map instead means every
        # recomputation of the verdict — here, in report.build(), in the summary —
        # reaches the same conclusion from the same durable evidence.
        status, reasons = cases.accept(
            document["matrix"],
            document["suites"],
            cleanup_ok=document["cleanup_ok"],
            stages=document["stages"],
        )
        document["status"], document["reasons"] = status, reasons
        close_attempt(document, status, now=self.clock())
        self.state.write(document)
        return status, reasons, failure

    def cleanup(self, ctx, document):
        stage = self.stages.get("cleanup")
        document["stages"]["cleanup"] = "running"
        try:
            ok = True if stage is None else bool(stage(ctx))
        except Exception as exc:
            document.setdefault("errors", []).append(
                {"stage": "cleanup", "type": type(exc).__name__}
            )
            ok = False
        outstanding = self.manifest.outstanding()
        if outstanding:
            ok = False
            document["cleanup_outstanding"] = [
                f"{entry['kind']}:{entry['id']}" for entry in outstanding
            ]
        document["stages"]["cleanup"] = "complete" if ok else "failed"
        document["cleanup_manifest_summary"] = self.manifest.summary()
        return ok


def timing(document, *, now):
    started = document.get("started_at") or now
    return {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "duration_seconds": int(now - started),
        "attempts": document.get("attempts") or [],
        "stages": document.get("stages") or {},
    }


def publish(document, cfg, out_dir, *, now, run_url=None):
    """Write the sanitized artifacts and the Actions summary."""
    matrix = document["matrix"]
    suites = tuple(document["suites"])
    payload = report.build(
        matrix=matrix,
        suites=suites,
        config={**cfg, "harness_commit": document.get("harness_commit")},
        evaluation_id=document["evaluation_id"],
        attempt_id=document["attempt_id"],
        cleanup_ok=document.get("cleanup_ok", False),
        timing=timing(document, now=now),
        correlation=document.get("correlation") or {},
        transcript=document.get("transcript") or (),
        preflight=document.get("preflight") or {},
        stages=document.get("stages") or {},
    )
    paths = report.write(out_dir, payload, matrix, document["evaluation_id"])
    text = report.summary(
        matrix,
        suites,
        document["evaluation_id"],
        cleanup_ok=document.get("cleanup_ok", False),
        revision=(document.get("preflight") or {}).get("deployed_revision")
        or document.get("expected_revision"),
        run_url=run_url,
        stages=document.get("stages") or {},
    )
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as handle:
            handle.write(text)
    return payload, paths, text


def status_view(document):
    """What `--mode status` prints. Sanitized: state itself is never published."""
    if document is None:
        return {"state": "absent"}
    counts = cases.tally(document.get("matrix") or {})
    return {
        "state": "present",
        "evaluation_id": document.get("evaluation_id"),
        "attempt_id": document.get("attempt_id"),
        "attempts": len(document.get("attempts") or []),
        "expected_revision": document.get("expected_revision"),
        "harness_commit": document.get("harness_commit"),
        "suites": document.get("suites"),
        "stages": document.get("stages"),
        "counts": counts,
        "status": document.get("status", "running"),
        "cleanup_ok": document.get("cleanup_ok", False),
        "cleanup_outstanding": document.get("cleanup_outstanding", []),
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="cli-uplift-eval", description="Repeatable CLI uplift evaluation (#5199)"
    )
    parser.add_argument(
        "--mode", choices=("start", "resume", "status", "cleanup"), default="start"
    )
    parser.add_argument(
        "--config", required=True, help="Path to the non-secret evaluation config JSON"
    )
    parser.add_argument(
        "--suite",
        action="append",
        default=None,
        help="Suite to run; repeatable. Default: full",
    )
    parser.add_argument("--state-dir", default=STATE_DIR)
    parser.add_argument(
        "--restore",
        action="store_true",
        help=(
            "Restore this evaluation's state from the durable store before "
            "running. Required to resume, report or clean up on a fresh runner, "
            "where the local state directory does not exist."
        ),
    )
    parser.add_argument(
        "--out-dir", default=None, help="Where report.json and results.xml are written"
    )
    parser.add_argument(
        "--evaluation-id",
        default=None,
        help="Reuse an existing immutable evaluation ID",
    )
    parser.add_argument(
        "--fault", choices=FAULTS, default="none", help="Bounded fault injection"
    )
    parser.add_argument("--run-url", default=None)
    parser.add_argument(
        "--announce-id-to",
        default=None,
        help=(
            "File to append `evaluation_id=<id>` to as soon as the ID exists, "
            "BEFORE any stage runs. In Actions this is $GITHUB_OUTPUT: the "
            "recovery job needs the ID of a run that was cancelled, and reading "
            "it from the final report means a cancelled run has no ID at all."
        ),
    )
    return parser.parse_args(argv)


def announce_evaluation_id(path, evaluation_id):
    """Publish the ID before the first mutation, so a cancelled run is findable.

    R6: the workflow read `evaluation_id` from the report the LAST step writes, so
    a run cancelled mid-journey — the one case that most needs recovery by ID —
    published nothing, and the recovery job fell back to age alone. Age cannot
    touch an instance younger than the TTL, so a cancellation minutes after launch
    left it running for four hours.

    Append, never truncate: in Actions this is $GITHUB_OUTPUT, which other steps
    of the same job also write to.
    """
    if not path:
        return None
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"evaluation_id={evaluation_id}\n")
    return evaluation_id


def resolve_stages(cfg, stages):
    """Decide which stage implementations this invocation runs.

    The entry point used to pass `stages or {}` straight through, so the workflow
    — which supplies nothing — got an empty mapping and the stage loop skipped
    everything. Production therefore builds the real mapping here; a test may pass
    a complete mapping (pure orchestration cases) or a `ports=`/`journeys=`
    override to drive the REAL stages against doubles.
    """
    if isinstance(stages, dict):
        # An explicit mapping is honoured as-is so orchestration tests can model a
        # crashing or slow stage. Required-stage coverage is enforced in run().
        return stages
    if stages is None:
        return stages_module.build_stages(cfg)
    if callable(stages):
        # A factory: given the config, produce the mapping. This is how the
        # offline integration tests exercise build_stages() with injected ports.
        return stages(cfg)
    raise RunnerError(
        "stages must be a mapping, a factory callable, or None for the live implementations"
    )


def restore_state(state, store, evaluation_id, cfg, *, holder):
    """Pull one evaluation's durable state onto this runner, validated.

    This is what makes resume, status and cleanup work across Actions runs. The
    local state directory lives in the runner's /tmp and dies with the runner, so
    without this a cancelled run's IAM roles, stacks, secrets and Cognito users
    have no record anywhere — and no tag-and-age instance sweep can find them.

    The lease is taken BEFORE the local write, so two jobs cannot both restore
    one evaluation and interleave deletions of the same resources.
    """
    if store is None:
        raise RunnerError(
            "--restore needs a durable state store; set state_bucket in the config"
        )
    check_evaluation_id(evaluation_id)
    store.claim(evaluation_id, holder)
    document, manifest = store.load(evaluation_id)
    statestore.check_restored(
        document,
        evaluation_id,
        cfg,
        fingerprint=fingerprint(cfg),
        harness_commit=config.HARNESS_COMMIT,
    )
    state.write(document)
    if manifest is not None:
        path = state.dir / MANIFEST_FILE
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        path.chmod(0o600)
    return document


def persist_state(store, document, state, *, holder=None):
    """Push state and manifest to the durable store, keyed by evaluation ID.

    Best-effort by design: a store outage must not fail a run that is otherwise
    healthy, and the local state plus the tag-based sweep remain. It is reported
    rather than silent, because losing durability means a later cancellation
    cannot be cleaned up by ID.
    """
    if store is None:
        return None
    manifest_path = state.dir / MANIFEST_FILE
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except ValueError:
            manifest = None
    try:
        store.save(document["evaluation_id"], document, manifest)
        return True
    except Exception as exc:
        print(
            json.dumps(
                {
                    "durable_state": "not_persisted",
                    "evaluation_id": document.get("evaluation_id"),
                    # Type only: a store error can quote a bucket policy or ARN.
                    "error_type": type(exc).__name__,
                }
            ),
            file=sys.stderr,
        )
        return False


def manifest_publisher(store, document):
    """A `Manifest.on_change` hook that pushes each intent to the durable store.

    R6: the run used to push the manifest exactly twice — once before the stages
    started, when it was empty, and once in an outer `finally`. Everything a
    journey recorded in between existed only in the runner's /tmp, so a
    cancellation or a lost runner mid-journey took the record of the IAM roles,
    CloudFormation stacks, secrets and Cognito users it had just created with it.
    Those kinds have no tag-and-age sweep behind them; the manifest IS their only
    route to deletion.

    So the push is a PRECONDITION of the mutation, not a report of it. A recorded
    intent whose push fails raises, which propagates out of `manifest.record()` —
    the line before the create call — and fails the stage. That is the desired
    outcome: refusing to create a resource we could not have recovered is strictly
    better than creating one nothing can find.

    Status changes are best-effort in the opposite direction. `mark()` fires
    during the sweep, and raising there would abandon the remaining deletions
    because the store was unreachable, which is the failure it exists to prevent.

    Returns None when there is no store, so the local manifest and the tag/age
    instance sweep are the (already-warned-about) guarantee.
    """
    if store is None:
        return None

    def publish(manifest, *, critical):
        try:
            store.save(document["evaluation_id"], document, manifest)
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "durable_state": "not_persisted",
                        "evaluation_id": document.get("evaluation_id"),
                        "critical": critical,
                        # Type only: a store error can quote a bucket policy or ARN.
                        "error_type": type(exc).__name__,
                    }
                ),
                file=sys.stderr,
            )
            if critical:
                raise RunnerError(
                    "A resource this run is about to create could not be recorded "
                    "durably, so nothing could later find it to delete. Refusing "
                    f"to proceed ({type(exc).__name__} from the state store)."
                ) from None
        return True

    return publish


def main(argv=None, stages=None, *, clock=time.time, store=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = config.load(args.config)
    suites = tuple(args.suite or ("full",))
    cases.resolve_suites(suites)  # reject an unknown suite before touching state
    # R3: the destination/provisioner/secret bindings the selected suites need,
    # checked here — before a lease, a state write or an instance exists.
    if args.mode in ("start", "resume"):
        config.require_bindings(cfg, suites)
    state = State(args.state_dir)
    out_dir = args.out_dir or str(Path(args.state_dir) / "out")
    now = clock()
    if store is None:
        store = statestore.default_store(cfg)
    # Identifies this runner in the lease, so a job can re-claim its own lease on
    # a retry instead of deadlocking against itself.
    holder = os.environ.get("GITHUB_RUN_ID") or f"local-{os.getpid()}"

    if args.restore:
        if not args.evaluation_id:
            raise RunnerError("--restore requires --evaluation-id")
        restore_state(state, store, args.evaluation_id, cfg, holder=holder)

    if args.mode == "status":
        print(json.dumps(status_view(state.read()), indent=2, sort_keys=True))
        return 0

    handle = state.lock()
    try:
        document = state.read()
        if args.mode == "cleanup":
            if document is None:
                print(json.dumps({"cleanup": "nothing to do", "state": "absent"}))
                return 0
            evaluation = Evaluation(
                cfg,
                state,
                resolve_stages(cfg, stages),
                clock=clock,
                on_manifest_change=manifest_publisher(store, document),
            )
            ok = evaluation.cleanup(evaluation.context(document), document)
            state.write(document)
            # Persist the post-sweep manifest, so a repeated cleanup (E15) sees
            # what this one already deleted instead of retrying every entry.
            persist_state(store, document, state)
            if store is not None and args.restore:
                store.release(document["evaluation_id"], holder)
            print(
                json.dumps(
                    {
                        "cleanup": "complete" if ok else "failed",
                        "evaluation_id": document["evaluation_id"],
                        "outstanding": document.get("cleanup_outstanding", []),
                    }
                )
            )
            return 0 if ok else 1

        if args.mode == "resume":
            if document is None:
                raise RunnerError("No run state to resume; use --mode start")
            resumable(document, cfg, suites)
            next_attempt(document, now=now)
        else:
            if document is not None and document.get("status") not in ("passed", None):
                raise RunnerError(
                    f"State for evaluation {document.get('evaluation_id')} already exists; use --mode resume or --mode cleanup"
                )
            evaluation_id = (
                check_evaluation_id(args.evaluation_id)
                if args.evaluation_id
                else new_evaluation_id(now, f"{now}:{os.getpid()}")
            )
            document = initial_state(
                cfg, suites, evaluation_id, now=now, fault=args.fault
            )
        state.write(document)
        # R6: publish the ID before the first stage, not from the final report. A
        # cancelled run never reaches the report, so reading it from there meant
        # the one case that most needs recovery by ID had no ID to recover by.
        announce_evaluation_id(args.announce_id_to, document["evaluation_id"])
        # Persist BEFORE the stages run, not only after. A run cancelled mid-
        # journey is exactly the case that needs its ID recoverable, and a state
        # first written at the end would not exist for it.
        if store is not None:
            store.claim(document["evaluation_id"], holder)
        # R6: durability of the run's identity is a PRECONDITION for mutating,
        # not a best-effort side effect. If the store is configured and the first
        # save fails, nothing downstream can find what this run creates — so it
        # must not create anything. With no store configured at all the operator
        # has already been warned by the config summary, and the tag/age sweep is
        # the remaining guarantee.
        if store is not None and not persist_state(store, document, state):
            raise RunnerError(
                "Durable state could not be written before the run started, so "
                "nothing this run creates would be recoverable by evaluation ID. "
                "Refusing to launch. Check the state bucket and its KMS key."
            )

        evaluation = Evaluation(
            cfg,
            state,
            resolve_stages(cfg, stages),
            clock=clock,
            on_manifest_change=manifest_publisher(store, document),
        )
        try:
            status, reasons, failure = evaluation.run(document)
        finally:
            # Under cancellation this is the last thing that runs in-process, so
            # it is what leaves the manifest recoverable by ID.
            persist_state(store, document, state)
        payload, paths, _text = publish(
            document, cfg, out_dir, now=clock(), run_url=args.run_url
        )
        print(
            json.dumps(
                {
                    "status": status,
                    "reasons": reasons,
                    "artifacts": paths,
                    "full_acceptance": payload["full_acceptance"],
                    # Surfaced so the step log names the stage that broke; the
                    # authoritative verdict is `status`, which already accounts
                    # for it via the persisted stage map.
                    "stage_failure": type(failure).__name__ if failure else None,
                },
                indent=2,
            )
        )
        # `status` is the single source of truth for the exit code: it already
        # incorporates cases, stages and cleanup, so this cannot disagree with the
        # published report.
        return 0 if status == cases.PASSED else 1
    finally:
        handle.close()


if __name__ == "__main__":
    sys.exit(main())
