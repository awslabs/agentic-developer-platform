"""CLI entrypoint; plan is the default and performs no network operations."""

import argparse
import json
import os
from pathlib import Path

from .config import Refusal, control_plane_mode, load, prepare_database_sql, validate
from .runner import Installer, local_lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--release-lock", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    # Control-plane-only mode: install the management surface without a workspace.
    # Workspace fields (workspace_cluster, workspace_namespace, workspace_id,
    # cluster_id, controller_ownership, workspace_access secret) are deferred.
    # The environment YAML may also set control_plane_only: true instead of this flag.
    parser.add_argument(
        "--control-plane-only",
        action="store_true",
        help="Install management surface only; workspace activation follows separately",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--rollback", type=Path, metavar="VERIFIED_RECEIPT")
    mode.add_argument("--cleanup", type=Path, metavar="RECEIPT")
    mode.add_argument("--recover-lock", action="store_true")
    # --prepare-database emits reviewable SQL for schema/role preparation.
    # It requires only --environment and --output; --release-lock is not used.
    mode.add_argument(
        "--prepare-database",
        action="store_true",
        help="Emit SQL preparation script for domain schemas and roles; no mutations",
    )
    parser.add_argument("--confirm-stopped", metavar="RUN_ID")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument(
        "--apply-preparation",
        action="store_true",
        help="With --prepare-database and a release lock, create owned credentials and authenticate all database roles",
    )
    args = parser.parse_args(argv)
    try:
        environment = load(args.environment)
        # control_plane_only: CLI flag takes precedence; environment YAML may also set it.
        control_plane_only = control_plane_mode(environment, args.control_plane_only)
        environment["control_plane_only"] = control_plane_only
        if args.apply_preparation and not args.prepare_database:
            raise Refusal("--apply-preparation requires --prepare-database")

        if args.prepare_database:
            # Validate only the environment inputs (no release lock, no workspace fields).
            # Pass lock=None to skip release-lock checks; use control_plane_only=True
            # to skip workspace fields since they are not needed for SQL prep.
            validate(environment, None, preparation=True)
            sql = prepare_database_sql(environment)
            args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
            sql_path = args.output / "prepare-database.sql"
            sql_path.write_text(sql)
            os.chmod(sql_path, 0o600)
            if args.apply_preparation:
                from .database_preparation import prepare

                if args.release_lock is None:
                    raise Refusal("Credential preparation requires --release-lock")
                lock = load(args.release_lock)
                validate(environment, lock, control_plane_only=control_plane_only)
                with local_lock(args.output):
                    previous_path = args.output / "receipt.json"
                    if previous_path.exists() and not args.resume:
                        raise Refusal(
                            "Preparation output already has a receipt; inspect it and use --resume or a new directory"
                        )
                    installer = Installer(
                        environment,
                        lock,
                        args.output.resolve(),
                        control_plane_only=control_plane_only,
                    )
                    if args.resume:
                        installer.resume(load(previous_path))
                        if installer.receipt.get(
                            "remote_lock"
                        ) or installer.receipt.get("temporary_preflight", {}).get(
                            "cleanup_required"
                        ):
                            raise Refusal(
                                "Prior preparation requires lock/temporary-namespace recovery before retry"
                            )
                    installer.phase(
                        "database-preparation",
                        lambda: prepare(
                            installer,
                            os.environ.get("SUPERPLANE_DATABASE_ADMIN_URL", ""),
                        ),
                    )
                    print(
                        json.dumps(
                            {
                                "status": installer.receipt["status"],
                                "receipt": str(installer.receipt_path),
                            }
                        )
                    )
                return 0
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "sql": str(sql_path),
                        "schemas": [
                            environment["database"]["schema"],
                            environment["database"]["skypilot_schema"],
                        ],
                        "note": "Review and execute prepare-database.sql as a privileged database user before running --preflight",
                    }
                )
            )
            return 0

        if args.release_lock is None:
            raise Refusal("--release-lock is required except for --prepare-database")
        lock = load(args.release_lock)
        validate(environment, lock, control_plane_only=control_plane_only)
        with local_lock(args.output):
            previous_path = args.output / "receipt.json"
            previous = load(previous_path) if args.resume else None
            if previous_path.exists() and not args.resume:
                raise Refusal(
                    "Output already has a receipt; choose a new directory or use --resume"
                )
            installer = Installer(
                environment,
                lock,
                args.output.resolve(),
                control_plane_only=control_plane_only,
            )
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
