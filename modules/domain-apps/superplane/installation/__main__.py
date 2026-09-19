"""CLI entrypoint; plan is the default and performs no network operations."""

import argparse
import json
import os
from pathlib import Path

from .config import Refusal, load, validate
from .runner import Installer, local_lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--release-lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--rollback", type=Path, metavar="VERIFIED_RECEIPT")
    mode.add_argument("--cleanup", type=Path, metavar="RECEIPT")
    mode.add_argument("--recover-lock", action="store_true")
    parser.add_argument("--confirm-stopped", metavar="RUN_ID")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args(argv)
    try:
        environment, lock = load(args.environment), load(args.release_lock)
        validate(environment, lock)
        with local_lock(args.output):
            previous_path = args.output / "receipt.json"
            previous = load(previous_path) if args.resume else None
            if previous_path.exists() and not args.resume:
                raise Refusal(
                    "Output already has a receipt; choose a new directory or use --resume"
                )
            installer = Installer(environment, lock, args.output.resolve())
            if previous:
                installer.resume(previous)
            else:
                installer.plan()
            token = os.environ.get("SUPERPLANE_VERIFICATION_TOKEN", "")
            if args.execute:
                installer.execute(args.approved_plan_sha256, token)
            elif args.rollback:
                installer.rollback(load(args.rollback), token)
            elif args.cleanup:
                installer.cleanup(load(args.cleanup))
            elif args.recover_lock:
                if not args.resume:
                    raise Refusal(
                        "--recover-lock requires --resume and the existing private receipt"
                    )
                installer.recover_lock(args.confirm_stopped)
            elif args.preflight:
                installer.preflight()
            print(
                json.dumps(
                    {
                        "status": installer.receipt["status"],
                        "receipt": str(installer.receipt_path),
                        "plan_sha256": installer.receipt.get("plan_sha256"),
                    }
                )
            )
        return 0
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 2
    except Exception:
        # Do not expose credentials through HTTP, SQL or cloud SDK errors.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Installation stage failed; inspect the private receipt. No completion is claimed.",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
