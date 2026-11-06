"""Explicit network-only revision of a stopped, disabled native installation.

Read-only by default. The prior receipt, workload release and images stay immutable.
A new receipt records the reviewed operator source and exact endpoint additions.
Normal preflight, saved-plan, identity, quiescence and activation gates all run.
"""

import argparse
import copy
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess

from . import native_egress
from .config import MODULE, Refusal, digest, identity, load, require, validate
from .native_egress_probe import verified_private_hosts
from .projection_recovery import PREFIX, WORKER, git, verifier_delta
from .runner import Installer, local_lock

MANIFESTS = PREFIX + "installation/manifests.py"
ENTRYPOINT = PREFIX + "installation/network_revision.py"
ALLOWED = {
    WORKER,
    MANIFESTS,
    ENTRYPOINT,
    PREFIX + "installation/native_egress.py",
    PREFIX + "installation/native_egress_probe.py",
    PREFIX + "installation/projection_recovery.py",
    PREFIX + "tests/installer_checks/test_policy_projection.py",
    PREFIX + "tests/installer_checks/test_private_service_egress.py",
    PREFIX + "tests/installer_checks/test_network_revision.py",
}
FINITE_JOB_FIX = """    # A finite Job must not inherit the Deployment's serving sidecars.
    # In particular, recovery-transport otherwise outlives successful migration
    # and bootstrap commands and prevents Kubernetes Job completion.
    pod["spec"]["containers"] = [container]
    mounts = {mount["name"] for mount in container.get("volumeMounts", [])}
    pod["spec"]["volumes"] = [
        volume for volume in pod["spec"].get("volumes", []) if volume["name"] in mounts
    ]
    pod["metadata"].get("annotations", {}).pop(
        "eks.amazonaws.com/skip-containers", None
    )
"""


def source_proof(original_revision):
    require(
        not git("status", "--porcelain", "--untracked-files=normal"),
        "Network revision checkout must be clean",
    )
    revision = git("rev-parse", "HEAD")
    require(revision != original_revision, "Separate operator source required")
    subprocess.run(
        [
            "git",
            "-C",
            str(MODULE),
            "merge-base",
            "--is-ancestor",
            original_revision,
            revision,
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    changed = set(git("diff", "--name-only", original_revision, revision).splitlines())
    require(
        changed <= ALLOWED and {WORKER, ENTRYPOINT} <= changed,
        "Operator source differs outside the reviewed network installer boundary",
    )
    verifier_delta(
        git("show", original_revision + ":" + WORKER),
        (MODULE / "installation/lifecycle_worker.py").read_text(),
    )
    original = git("show", original_revision + ":" + MANIFESTS)
    current = (MODULE / "installation/manifests.py").read_text().strip()
    require(
        current == original
        or (
            current.count(FINITE_JOB_FIX) == 1
            and current.replace(FINITE_JOB_FIX, "", 1) == original
        ),
        "Manifest changes exceed the finite Job sidecar fix",
    )
    return {
        "original_source_revision": original_revision,
        "operator_source_revision": revision,
        "changed_file_sha256": {
            path: hashlib.sha256(
                (MODULE / path.removeprefix(PREFIX)).read_bytes()
            ).hexdigest()
            for path in sorted(changed)
        },
    }


def review_inputs(environment, release, previous):
    require(
        previous.get("status") == "failed"
        and not previous.get("remote_lock")
        and not previous.get("lock_attempt")
        and not previous.get("temporary_preflight", {}).get("cleanup_required")
        and previous.get("public_route_enabled") is False
        and previous.get("adapter_stage", {}).get("state") == "disabled-restored"
        and previous.get("lifecycle_foundations", {}).get("retained") is True
        and previous.get("bootstrap", {}).get("token_secret_removed") is True
        and "adapter-stage-quiescence" in previous.get("completed", []),
        "Prior installation must be stopped, disabled and lock-recovered",
    )
    require(
        release == previous.get("release_lock")
        and digest(release) == previous.get("release_id"),
        "Workload release changed",
    )
    prior_env = previous["environment"]
    candidate = copy.deepcopy(environment)
    native_egress.validate(candidate)
    require(
        "interface_endpoints" not in prior_env["paid_worker"]["egress"],
        "This revision only adds previously absent interface endpoints",
    )
    endpoints = candidate["paid_worker"]["egress"].pop("interface_endpoints", None)
    native_egress.validate_interface_endpoints(endpoints)
    require(
        candidate == prior_env and identity(environment) == previous["installation_id"],
        "Revision may only add exact STS/SQS endpoint declarations",
    )
    return {
        "version": 1,
        "prior_run_id": previous["run_id"],
        "installation_id": previous["installation_id"],
        "prior_receipt_sha256": digest(previous),
        "prior_environment_sha256": digest(prior_env),
        "environment_sha256": digest(environment),
        "release_sha256": digest(release),
        "prior_plan_sha256": previous["plan_sha256"],
        "interface_endpoints": endpoints,
        "source": source_proof(release["source_revision"]),
    }


class NetworkRevisionInstaller(Installer):
    def target(self, verify_source=True):
        require(
            source_proof(self.lock["source_revision"])
            == self.revision_review["source"],
            "Reviewed operator source changed",
        )
        super().target(verify_source=False)
        # Live endpoint identity must pass before any network rule mutation.
        self.receipt["verified_interface_endpoints"] = verified_private_hosts(self)
        self.save()
        if verify_source:
            self.verify_image_sources()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--release-lock", required=True, type=Path)
    parser.add_argument("--prior-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare",
        action="store_true",
        help="Write an offline revision receipt before provisioning its exact preflight namespace",
    )
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--recover-lock", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--confirm-stopped")
    parser.add_argument("--approved-review-sha256")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args()
    try:
        require(
            args.output.resolve() != args.prior_output.resolve(),
            "Revision requires a new output directory",
        )
        with local_lock(args.prior_output), local_lock(args.output):
            environment, release = load(args.environment), load(args.release_lock)
            validate(
                environment,
                release,
                control_plane_only=environment.get("control_plane_only", False),
            )
            previous = load(args.prior_output / "receipt.json")
            review = review_inputs(environment, release, previous)
            review_sha256 = digest(review)
            if not args.execute and not args.recover_lock and not args.prepare:
                print(
                    json.dumps(
                        {"review": review, "review_sha256": review_sha256},
                        sort_keys=True,
                    )
                )
                return 0
            require(
                args.approved_review_sha256 == review_sha256
                and re.fullmatch(r"[a-f0-9]{64}", args.approved_plan_sha256 or ""),
                "Exact operator review and saved-plan approvals required",
            )
            token = os.environ.get("SUPERPLANE_VERIFICATION_TOKEN", "")
            require(
                args.recover_lock or args.prepare or bool(token),
                "Current verification token required",
            )
            installer = NetworkRevisionInstaller(
                environment, release, args.output.resolve()
            )
            installer.revision_review = review
            record = {"review_sha256": review_sha256, "review": review}
            path = args.output / "receipt.json"
            require(
                path.exists() == args.resume, "Existing output requires explicit resume"
            )
            if args.resume:
                current = load(path)
                require(
                    current.get("network_configuration_revision") == record,
                    "Revision receipt provenance changed",
                )
                installer.resume(current)
            else:
                require(not args.recover_lock, "Lock recovery requires resume")
                installer.receipt["network_configuration_revision"] = record
                installer.plan()
            if args.prepare:
                require(not args.resume, "Prepare requires a new output directory")
            elif args.recover_lock:
                installer.recover_lock(args.confirm_stopped)
            else:
                require(
                    not installer.receipt.get("remote_lock")
                    and not installer.receipt.get("lock_attempt")
                    and not installer.receipt.get("temporary_preflight", {}).get(
                        "cleanup_required"
                    ),
                    "Complete supported lock recovery first",
                )
                installer.execute(args.approved_plan_sha256, token)
            print(
                json.dumps(
                    {"status": installer.receipt["status"], "run_id": installer.run_id}
                )
            )
            return 0
    except Refusal as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}))
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Preserve private revision receipt and logs",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
