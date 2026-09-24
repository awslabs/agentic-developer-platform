#!/usr/bin/env python3
"""Ownership-proving ledger for the Wave 2 fixture (issue #3968).

WHAT THIS FIXES
---------------
The published scripts recorded every resource into the deletion ledger with
``run_bound: true`` *before* creating it, and created resources with
``kubectl apply`` / ``sqs create-queue``. Both of those calls succeed against a
resource that already exists:

  * ``kubectl apply`` ADOPTS a pre-existing object of the same name, mutating it.
  * ``sqs create-queue`` returns the EXISTING queue's URL when the name is taken
    and the attributes match.

Combined with a pre-recorded ``run_bound: true``, a name collision would have
authorised cleanup to delete somebody else's resource. A name prefix and a
self-asserted boolean are not ownership.

WHAT OWNERSHIP ACTUALLY IS HERE
-------------------------------
Two independent facts, both recorded from the SERVER's response at creation:

1. **Exclusive creation.** ``kubectl create`` (not ``apply``) fails with
   AlreadyExists, and we refuse rather than continue. For SQS, we probe for the
   name first and refuse if it resolves, then create.

2. **A server-assigned identity.** Kubernetes returns a ``metadata.uid`` that is
   unique to that object instance -- delete and recreate the same name and the
   uid differs. SQS has no uid, so we tag the queue with a run nonce and verify
   the tag at teardown. Either way, teardown re-reads the identity and deletes
   ONLY if it still matches what creation recorded. A same-name replacement is
   therefore left alone instead of destroyed.

The run nonce is generated once per run and is not derived from a clock, so two
runs cannot collide even if started in the same second.

Ledger writes are ATOMIC (temp file + ``os.replace``). The published version
rewrote the ledger in place, so an interruption mid-write left invalid JSON --
which is the one file that must still parse after an interrupted run.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

LEDGER_VERSION = 2


# ---------------------------------------------------------------------------
# nonce
# ---------------------------------------------------------------------------
def new_nonce() -> str:
    """A run nonce that does not depend on a clock.

    Two runs started in the same second must not produce the same nonce, so a
    timestamp is not sufficient on its own.
    """
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# atomic ledger IO
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, payload: dict) -> None:
    """Write JSON so an interrupted write cannot leave an unparseable ledger.

    The ledger is the only record of what must be deleted. A truncated ledger
    means an operator cannot clean up at all, so this is written to a temp file
    in the same directory and renamed over the target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_ledger(path: Path, *, run_id: str | None = None, account_id: str | None = None) -> dict:
    """Load a ledger and REFUSE a foreign one.

    The published version accepted any existing ledger silently, so a stale file
    from another run (or another account) would drive this run's teardown. That
    is how a previous run's surviving resources get deleted by a later run that
    never created them -- or worse, how this run's resources go unrecorded
    because the ledger it appended to belongs elsewhere.
    """
    if not path.is_file():
        raise FileNotFoundError(f"ledger not found: {path}")
    try:
        led = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"ledger {path} is not valid JSON ({exc}). An interrupted write can cause "
            "this; the resources it described must be found and removed by hand. Do not "
            "delete this file -- it is the only record."
        ) from exc
    if not isinstance(led, dict):
        raise ValueError(f"ledger {path} must be a JSON object, got {type(led).__name__}")

    if run_id is not None and led.get("run_id") != run_id:
        raise ValueError(
            f"ledger {path} belongs to run {led.get('run_id')!r}, not {run_id!r}. Refusing "
            "to append to or tear down a foreign run's ledger: its entries would authorise "
            "deleting resources this run did not create. Use a fresh ledger path."
        )
    if account_id is not None and led.get("account_id") != account_id:
        raise ValueError(
            f"ledger {path} was written against account {led.get('account_id')!r}, but this "
            f"session is on {account_id!r}. Refusing: the same resource name in two accounts "
            "is two different resources."
        )
    if led.get("ledger_version") != LEDGER_VERSION:
        raise ValueError(
            f"ledger {path} is version {led.get('ledger_version')!r}, this tool writes "
            f"v{LEDGER_VERSION}. A v1 ledger records `run_bound: true` with no server-assigned "
            "identity, so its entries cannot prove ownership and must not drive a teardown."
        )
    return led


def init_ledger(path: Path, run_id: str, account_id: str, region: str, nonce: str) -> dict:
    """Create a ledger, or return the existing one for this same run."""
    if path.is_file():
        led = load_ledger(path, run_id=run_id, account_id=account_id)
        if led.get("run_nonce") != nonce:
            raise ValueError(
                f"ledger {path} has run_nonce {led.get('run_nonce')!r} but this invocation "
                f"generated {nonce!r}. Resuming a run requires reusing its nonce (pass "
                "--nonce from the ledger); a new nonce means a new run and needs a new ledger."
            )
        return led
    led = {
        "ledger_version": LEDGER_VERSION,
        "run_id": run_id,
        "run_nonce": nonce,
        "account_id": account_id,
        "region": region,
        "synthetic_rows": [],
        "k8s": [],
        "queues": [],
    }
    _atomic_write(path, led)
    return led


# ---------------------------------------------------------------------------
# recording, with ownership evidence
# ---------------------------------------------------------------------------
def record_k8s(
    path: Path,
    *,
    run_id: str,
    account_id: str,
    kind: str,
    name: str,
    namespace: str,
    uid: str,
    created_by_this_run: bool,
) -> None:
    """Record a Kubernetes object AFTER creation, with the server-assigned uid.

    ``uid`` is required and must be non-empty: it is the whole ownership proof.
    Recording an object without one would produce a ledger entry that teardown
    cannot verify, which is the defect this replaces.
    """
    if not uid:
        raise ValueError(
            f"refusing to record {kind}/{name}: no metadata.uid was captured. Without the "
            "server-assigned uid, teardown cannot distinguish this object from a later "
            "object of the same name, so it must not be authorised for deletion."
        )
    if not created_by_this_run:
        raise ValueError(
            f"refusing to record {kind}/{name} as deletable: it was not created by this run. "
            "Adopting a pre-existing resource and then deleting it is the exact failure this "
            "guard exists to prevent."
        )
    led = load_ledger(path, run_id=run_id, account_id=account_id)
    entry = {
        "kind": kind,
        "name": name,
        "namespace": namespace,
        "uid": uid,
        "delete": True,
        "created_by_this_run": True,
    }
    existing = [
        e for e in led["k8s"]
        if e.get("kind") == kind and e.get("name") == name and e.get("namespace") == namespace
    ]
    if existing:
        if existing[0].get("uid") != uid:
            raise ValueError(
                f"{kind}/{name} in {namespace} is already recorded with uid "
                f"{existing[0].get('uid')!r} but was just created with uid {uid!r}. Two "
                "different object instances share a name in one run's ledger; resolve by hand."
            )
        return
    led["k8s"].append(entry)
    _atomic_write(path, led)


def record_queue(
    path: Path,
    *,
    run_id: str,
    account_id: str,
    name: str,
    url: str,
    nonce: str,
    created_by_this_run: bool,
) -> None:
    """Record an SQS queue AFTER creation.

    SQS has no uid, so the run nonce written as a queue TAG is the ownership
    evidence: teardown reads the tag back and deletes only on a match.
    """
    if not created_by_this_run:
        raise ValueError(
            f"refusing to record queue {name} as deletable: create-queue returned an "
            "EXISTING queue rather than creating one. SQS CreateQueue is idempotent on a "
            "matching name, so this is indistinguishable from adoption -- and the probe "
            "deployment's queue is exactly the resource that must never be deleted."
        )
    if not nonce:
        raise ValueError(f"refusing to record queue {name}: no run nonce to verify at teardown")
    led = load_ledger(path, run_id=run_id, account_id=account_id)
    entry = {
        "name": name,
        "url": url,
        "owner_tag_nonce": nonce,
        "delete": True,
        "created_by_this_run": True,
    }
    if any(e.get("name") == name for e in led["queues"]):
        return
    led["queues"].append(entry)
    _atomic_write(path, led)


def record_row(path: Path, *, run_id: str, account_id: str, event_id: str, arrived_at: str) -> None:
    """Record a synthetic row by BOTH key halves.

    A row recorded with only ``event_id`` cannot be deleted safely: a delete
    keyed on the hash alone would need a guessed range key, and a guess can match
    an unrelated item.
    """
    if not event_id or not arrived_at:
        raise ValueError(
            f"refusing to record row event_id={event_id!r} arrived_at={arrived_at!r}: both "
            "key halves are required. A partial key cannot be deleted without guessing."
        )
    led = load_ledger(path, run_id=run_id, account_id=account_id)
    entry = {"event_id": event_id, "arrived_at": arrived_at}
    if entry in led["synthetic_rows"]:
        return
    led["synthetic_rows"].append(entry)
    _atomic_write(path, led)


# ---------------------------------------------------------------------------
# CLI — shell steps call these subcommands
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("nonce", help="generate a run nonce")

    p = sub.add_parser("init", help="create or validate the ledger")
    p.add_argument("--ledger", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--nonce", required=True)

    p = sub.add_parser("record-k8s", help="record a created k8s object with its uid")
    p.add_argument("--ledger", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--uid", required=True)

    p = sub.add_parser("record-queue", help="record a created queue with its owner nonce")
    p.add_argument("--ledger", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--nonce", required=True)

    p = sub.add_parser("record-row", help="record a synthetic row by both key halves")
    p.add_argument("--ledger", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--account-id", required=True)
    p.add_argument("--event-id", required=True)
    p.add_argument("--arrived-at", required=True)

    p = sub.add_parser("validate", help="validate a ledger's run/account and shape")
    p.add_argument("--ledger", required=True)
    p.add_argument("--run-id")
    p.add_argument("--account-id")

    args = parser.parse_args(argv)

    try:
        if args.cmd == "nonce":
            print(new_nonce())
            return 0
        if args.cmd == "init":
            init_ledger(Path(args.ledger), args.run_id, args.account_id, args.region, args.nonce)
            print(f"ledger ready: {args.ledger}")
            return 0
        if args.cmd == "record-k8s":
            record_k8s(
                Path(args.ledger), run_id=args.run_id, account_id=args.account_id,
                kind=args.kind, name=args.name, namespace=args.namespace,
                uid=args.uid, created_by_this_run=True,
            )
            print(f"ledger += {args.kind}/{args.name} uid={args.uid}")
            return 0
        if args.cmd == "record-queue":
            record_queue(
                Path(args.ledger), run_id=args.run_id, account_id=args.account_id,
                name=args.name, url=args.url, nonce=args.nonce, created_by_this_run=True,
            )
            print(f"ledger += queue/{args.name}")
            return 0
        if args.cmd == "record-row":
            record_row(
                Path(args.ledger), run_id=args.run_id, account_id=args.account_id,
                event_id=args.event_id, arrived_at=args.arrived_at,
            )
            print(f"ledger += row {args.event_id}")
            return 0
        if args.cmd == "validate":
            led = load_ledger(
                Path(args.ledger),
                run_id=args.run_id,
                account_id=args.account_id,
            )
            print(json.dumps({
                "run_id": led["run_id"],
                "account_id": led["account_id"],
                "rows": len(led["synthetic_rows"]),
                "k8s": len(led["k8s"]),
                "queues": len(led["queues"]),
            }, indent=2))
            return 0
    except (ValueError, FileNotFoundError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
