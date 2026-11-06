"""Reviewed installer-only continuation for Kubernetes empty-policy serialization.

The workload release remains immutable. Use a clean checkout derived from that
release with only the narrow verifier fix, this entrypoint and its tests. Every
normal preflight, identity, plan, lock, activation and verification gate still runs.
The default command is read-only and prints the review digest required to execute.
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .config import MODULE, Refusal, digest, load, require, validate
from .runner import Installer, local_lock


PREFIX = "modules/domain-apps/superplane/"
WORKER = PREFIX + "installation/lifecycle_worker.py"
ALLOWED = {
    WORKER,
    PREFIX + "installation/projection_recovery.py",
    PREFIX + "tests/installer_checks/test_policy_projection.py",
}


def git(*args):
    return subprocess.check_output(
        ["git", "-C", str(MODULE), *args], text=True, timeout=30
    ).strip()


def verifier_delta(original, current):
    """No other original installer statement may change in a recovery source."""
    before, after = ast.parse(original), ast.parse(current)
    helper = [
        node
        for node in after.body
        if isinstance(node, ast.FunctionDef) and node.name == "projection_matches"
    ]
    require(len(helper) == 1, "Exact projection helper required")
    after.body.remove(helper[0])
    calls = 0
    for node in after.body:
        if isinstance(node, ast.FunctionDef) and node.name == "installed_snapshot":
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "projection_matches"
                ):
                    require(
                        len(child.args) == 2
                        and [ast.dump(arg) for arg in child.args]
                        == [
                            ast.dump(ast.Name(id=name, ctx=ast.Load()))
                            for name in ("actual", "desired")
                        ]
                        and not child.keywords,
                        "Unexpected projection verification call",
                    )
                    child.func.id = "contains"
                    calls += 1
    require(
        calls == 1 and ast.dump(before) == ast.dump(after),
        "Recovery changed other worker behavior",
    )


def source_proof(original_revision):
    require(
        not git("status", "--porcelain", "--untracked-files=normal"),
        "Recovery checkout must be clean",
    )
    revision = git("rev-parse", "HEAD")
    require(revision != original_revision, "Recovery needs its own recorded source")
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
        changed <= ALLOWED
        and WORKER in changed
        and PREFIX + "installation/projection_recovery.py" in changed,
        "Recovery source differs outside the narrow verifier boundary",
    )
    original = git("show", original_revision + ":" + WORKER)
    current = (MODULE / "installation/lifecycle_worker.py").read_text()
    verifier_delta(original, current)
    return {
        "original_source_revision": original_revision,
        "operator_source_revision": revision,
        "changed_paths": sorted(changed),
        "worker_verifier_sha256": hashlib.sha256(current.encode()).hexdigest(),
    }


def review_inputs(environment, release, previous):
    require(
        previous.get("status") == "failed"
        and not previous.get("remote_lock")
        and not previous.get("temporary_preflight", {}).get("cleanup_required")
        and previous.get("public_route_enabled") is False
        and previous.get("adapter_stage", {}).get("state") == "disabled-restored"
        and previous.get("lifecycle_foundations", {}).get("retained") is True
        and previous.get("bootstrap", {}).get("token_secret_removed") is True,
        "Complete supported lock recovery for the stopped disabled installation first",
    )
    require(
        environment == previous.get("environment")
        and release == previous.get("release_lock"),
        "Immutable release or environment changed",
    )
    require(
        "adapter-stage-quiescence" in previous.get("completed", []),
        "Recovery requires prior verified rollout and quiescence",
    )
    return {
        "version": 1,
        "run_id": previous["run_id"],
        "installation_id": previous["installation_id"],
        "environment_sha256": digest(environment),
        "release_sha256": digest(release),
        "receipt_sha256": digest(previous),
        "approved_plan_sha256": previous["plan_sha256"],
        "source": source_proof(release["source_revision"]),
    }


class ProjectionRecoveryInstaller(Installer):
    def target(self, verify_source=True):
        # Replace only the original same-checkout check with an explicit,
        # review-pinned installer delta. All rendered/runtime/infra sources are
        # byte-identical to the original release outside that verifier helper.
        require(
            source_proof(self.lock["source_revision"]) == self.recovery_source,
            "Reviewed recovery source changed",
        )
        super().target(verify_source=False)
        if verify_source:
            self.verify_image_sources()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, type=Path)
    parser.add_argument("--release-lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-review-sha256")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args()
    try:
        with local_lock(args.output):
            environment, release = load(args.environment), load(args.release_lock)
            validate(
                environment,
                release,
                control_plane_only=environment.get("control_plane_only", False),
            )
            previous = load(args.output / "receipt.json")
            review = review_inputs(environment, release, previous)
            review_sha256 = digest(review)
            if not args.execute:
                print(
                    json.dumps(
                        {"review": review, "review_sha256": review_sha256},
                        sort_keys=True,
                    )
                )
                return 0
            require(
                args.approved_review_sha256 == review_sha256
                and args.approved_plan_sha256 == review["approved_plan_sha256"],
                "Exact review and Terraform plan approvals required",
            )
            token = os.environ.get("SUPERPLANE_VERIFICATION_TOKEN", "")
            require(bool(token), "Current authenticated verification token required")
            installer = ProjectionRecoveryInstaller(
                environment, release, args.output.resolve()
            )
            installer.recovery_source = review["source"]
            installer.resume(previous)
            installer.receipt.setdefault(
                "installer_verification_recoveries", []
            ).append({"review_sha256": review_sha256, **review["source"]})
            installer.save()
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
                    "reason": "Preserve the private receipt; projection recovery did not complete",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
