"""Create-only recovery credentials; no worker, admission or public route writes."""

import argparse
import hashlib
import json
from pathlib import Path

from .config import Refusal, digest, load, require, validate
from .database_preparation import owned_secret
from .recovery_transport import endpoint, generate, validate_material
from .runner import Installer, local_lock


def prepare(installer):
    env = installer.env
    installer.verify_deployment_identity()
    with installer.exclusive():
        # Use the existing create-only owner; never rotate/adopt another secret.
        original = env["secrets"]
        try:
            env["secrets"] = dict(
                original,
                recovery_tls=env["recovery_transport"]["tls_secret"],
                recovery_credential=env["recovery_transport"]["credential_secret"],
            )
            tls, tls_version = owned_secret(
                installer, "recovery_tls", lambda: generate(env)[0]
            )
            try:
                expected = {
                    "authorization": json.loads(tls["submitter"])["credential"],
                    "ca_pem": tls["ca.pem"],
                }
            except (ValueError, KeyError, TypeError):
                raise Refusal("Stored recovery TLS material is invalid") from None
            validate_material(env, tls, expected)
            credential, version = owned_secret(
                installer, "recovery_credential", lambda: expected
            )
            validate_material(env, tls, credential)
        finally:
            env["secrets"] = original
        installer.receipt["recovery_transport"] = {
            "endpoint": endpoint(env),
            "tls_secret_version": tls_version,
            "credential_secret_version": version,
            "listener_ready": False,
        }
        installer.receipt["status"] = "recovery-credentials-prepared"
        installer.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prepare-recovery-transport", action="store_true", required=True
    )
    for name in ("environment", "release-lock", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--recover-lock", action="store_true")
    parser.add_argument("--confirm-stopped")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args(argv)
    try:
        env, lock = load(args.environment), load(args.release_lock)
        validate(env, lock, control_plane_only=True)
        require(
            env.get("recovery_transport"),
            "Select recovery_transport before preparing its credentials",
        )
        with local_lock(args.output):
            installer = Installer(
                env, lock, args.output.resolve(), control_plane_only=True
            )
            plan = {
                "version": 1,
                "installation_id": installer.owner,
                "environment_sha256": digest(installer.env),
                "release_sha256": digest(lock),
                "secrets": env["recovery_transport"],
                "endpoint": endpoint(env),
                "source_sha256": hashlib.sha256(
                    b"".join(
                        Path(__file__).with_name(n).read_bytes()
                        for n in (
                            "recovery_preparation.py",
                            "recovery_transport.py",
                            "database_preparation.py",
                            "config.py",
                            "runner.py",
                        )
                    )
                ).hexdigest(),
            }
            if installer.receipt_path.exists():
                prior = load(installer.receipt_path)
                require(
                    args.resume and prior.get("recovery_plan") == plan,
                    "Resume requires the original recovery plan",
                )
                require(
                    args.recover_lock
                    or not prior.get("remote_lock")
                    and not prior.get("lock_attempt"),
                    "Recover the existing installation lock before resuming",
                )
                if prior.get("status") == "recovery-credentials-prepared":
                    require(
                        digest(prior.get("environment")) == digest(installer.env)
                        and digest(prior.get("release_lock")) == installer.release
                        and prior.get("installation_id") == installer.owner,
                        "Completed recovery receipt differs from the installation",
                    )
                    installer.run_id, installer.receipt = prior["run_id"], prior
                else:
                    installer.resume(prior)
            else:
                require(
                    not args.resume and not args.recover_lock,
                    "Resume requires an existing recovery receipt",
                )
            installer.receipt.update(
                mode="recovery-transport", recovery_plan=plan, plan_sha256=digest(plan)
            )
            installer.save()
            if args.recover_lock:
                installer.recover_lock(args.confirm_stopped)
            elif args.execute:
                require(
                    args.approved_plan_sha256 == digest(plan),
                    "Review the exact recovery preparation plan",
                )
                installer.phase("recovery-credentials", lambda: prepare(installer))
            print(
                json.dumps(
                    {
                        "status": installer.receipt["status"],
                        "plan_sha256": digest(plan),
                        "receipt": str(installer.receipt_path),
                        "listener_ready": False,
                    }
                )
            )
            return 0
    except Refusal as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Recovery preparation failed; inspect private receipts",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
