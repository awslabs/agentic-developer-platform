"""Read-only reconciliation of protected tools releases against observed runs."""

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .release_scope import operator_sequence
    from .release_window import verify_window
else:
    from release_scope import operator_sequence
    from release_window import verify_window


REVISION = re.compile(r"[a-f0-9]{40}\Z")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")


def run_order(run):
    return (run.get("id") if type(run.get("id")) is int else 0,
            run.get("run_attempt") if type(run.get("run_attempt")) is int else 0)


def selected_source(run, receipt):
    """Use authenticated dispatch inputs or a run-bound workflow receipt, never guess from main."""
    head = run.get("head_sha")
    source = receipt.get("source_revision")
    if not isinstance(source, str) or not REVISION.fullmatch(source):
        return None
    inputs = run.get("inputs") or {}
    values = [inputs[name] for name in ("source_sha", "manual_source_revision", "adp_source_revision")
              if isinstance(inputs, dict) and inputs.get(name)]
    if values and (len(set(values)) != 1 or values[0] != source):
        return None
    if source != head and not values and receipt.get("workflow_revision") != head:
        return None
    if run.get("event") == "push" and source != head:
        return None
    return source


def receipt_matches(receipt, run, component, workflow, manifest, environment, account, is_ancestor):
    selected = selected_source(run, receipt)
    return (selected is not None and is_ancestor(selected, run["head_sha"])
            and (component != "tool-adapters" or receipt.get("evidence_type") == "independent-runtime")
            and receipt.get("component") == component
            and receipt.get("workflow") == workflow
            and receipt.get("run_id") == run.get("id")
            and receipt.get("run_attempt") == run.get("run_attempt")
            and receipt.get("environment") == environment
            and receipt.get("account") == account
            and receipt.get("mode") == "apply"
            and receipt.get("component_digests") == manifest
            and receipt.get("runtime_digests") == manifest)



def run_serves_component(run, component):
    if run.get("workflow") != "tools-platform-deploy.yml":
        return True
    inputs = run.get("inputs") or {}
    if component == "tool-adapters":
        return (run.get("event") == "workflow_dispatch"
                and run.get("run_attempt") == 1
                and isinstance(inputs, dict)
                and inputs.get("mode") == "release"
                and inputs.get("stage") == "tool-adapters"
                and isinstance(inputs.get("adp_correlation"), str)
                and re.fullmatch(r"orch:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                                 inputs["adp_correlation"]) is not None)
    return not isinstance(inputs, dict) or inputs.get("stage") != "tool-adapters"


def previous_group_verified(previous, *, source, environment, account, is_ancestor):
    if not isinstance(previous, dict) or set(previous) != {
        "manifest", "readback", "gateway_image", "facade_image"
    }:
        return False
    manifest = previous["manifest"]
    if (not isinstance(manifest, dict) or manifest.get("environment") != environment
            or manifest.get("account") != account
            or not isinstance(manifest.get("components"), dict)
            or "platform targets" not in manifest["components"]
            or not isinstance(manifest.get("source_revision"), str)
            or not REVISION.fullmatch(manifest["source_revision"])
            or not is_ancestor(manifest["source_revision"], source)
            or not isinstance(manifest.get("route_group"), dict)
            or manifest["route_group"].get("status") not in {"legacy", "active"}
            or not callable(previous["readback"])):
        return False
    try:
        verify_window(manifest, previous["readback"], source=manifest["source_revision"],
                      account=account, gateway_image=previous["gateway_image"],
                      facade_image=previous["facade_image"])
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return True


def reconcile(steps, source, main, manifest, runs, receipts, is_ancestor, *, environment, account, previous=None):
    """Return operator actions, never dispatch a workflow or call a provider."""
    if not (isinstance(source, str) and REVISION.fullmatch(source)
            and isinstance(main, str) and REVISION.fullmatch(main)
            and is_ancestor(source, main)
            and environment in ("dev", "prod") and isinstance(account, str)
            and re.fullmatch(r"[0-9]{12}", account)
            and isinstance(manifest, dict)
            and (manifest or any(step.get("component") == "protected bootstrap" for step in steps))
            and all(isinstance(name, str) and isinstance(images, dict) and images
                    and all(isinstance(image, str) and isinstance(digest, str) and DIGEST.fullmatch(digest)
                            for image, digest in images.items()) for name, images in manifest.items())
            and isinstance(runs, list) and isinstance(receipts, list)):
        raise ValueError("Reviewed main ancestry, target and immutable component manifest required")
    selected = [step for step in steps if step["workflow"] is not None]
    if not selected and any(step["component"] == "protected bootstrap" for step in steps):
        return {"status": "plan-only", "source_revision": source, "main_revision": main,
                "decisions": [{"component": "protected bootstrap", "action": "blocked",
                               "reason": "Coordinator must review existing state and scoped bootstrap before apply"}]}
    if {step["component"] for step in selected} != set(manifest):
        raise ValueError("Exact selected component manifest required; no omitted images or targets")
    decisions = []
    for step in selected:
        component, workflow = step["component"], step["workflow"]
        relevant = [run for run in runs if isinstance(run, dict) and run.get("workflow") == workflow
                    and run_serves_component(run, component)
                    and isinstance(run.get("head_sha"), str) and REVISION.fullmatch(run["head_sha"])
                    and is_ancestor(source, run["head_sha"])
                    and is_ancestor(run["head_sha"], main)
                    and run.get("event") in ("push", "workflow_dispatch")]
        latest = None
        for run in relevant:
            if latest is None or (run["head_sha"] != latest["head_sha"]
                                  and is_ancestor(latest["head_sha"], run["head_sha"])):
                latest = run
            elif run["head_sha"] == latest["head_sha"] and run_order(run) > run_order(latest):
                latest = run
        decision = {"component": component, "workflow": workflow, "action": "blocked"}
        if latest is not None:
            decision["source_revision"] = latest["head_sha"]
            decision["run_id"] = latest.get("id")
            decision["run_attempt"] = latest.get("run_attempt")
            if not (type(latest.get("id")) is int and latest["id"] > 0
                    and type(latest.get("run_attempt")) is int and latest["run_attempt"] > 0):
                decision["reason"] = "Verified run and attempt identity unavailable"
            elif latest.get("status") != "completed":
                decision["reason"] = "Automatic or newer release still running; observe before recovery"
            elif latest.get("conclusion") == "success":
                matching = [receipt for receipt in receipts if isinstance(receipt, dict)
                            and receipt_matches(receipt, latest, component, workflow, manifest,
                                                environment, account, is_ancestor)]
                if matching:
                    deployed_source = selected_source(latest, matching[0])
                    newer_verified = any(
                        isinstance(older_receipt, dict)
                        and receipt_matches(older_receipt, older_run, component, workflow, manifest,
                                            environment, account, is_ancestor)
                        and selected_source(older_run, older_receipt) != deployed_source
                        and is_ancestor(deployed_source, selected_source(older_run, older_receipt))
                        for older_run in relevant if older_run is not latest
                        and older_run.get("status") == "completed" and older_run.get("conclusion") == "success"
                        for older_receipt in receipts)
                    if newer_verified:
                        decision["reason"] = "A newer compatible source is already deployed; never downgrade implicitly"
                    elif not is_ancestor(source, deployed_source):
                        decision["reason"] = "Selected source predates reviewed release; never downgrade implicitly"
                    else:
                        decision["source_revision"] = deployed_source
                        decision["workflow_revision"] = latest["head_sha"]
                        decision["action"] = "reuse"
                        decision["reason"] = "Compatible applied runtime receipt matches the observed run"
                else:
                    decision["reason"] = "Successful run lacks a compatible independent runtime receipt"
            elif latest["head_sha"] != source:
                decision["reason"] = "Newer main release is unverified; never downgrade implicitly"
            elif source != main:
                decision["reason"] = "Main advanced; recovery at an older source is not authorized"
            elif component == "tool-adapters" and not previous_group_verified(
                previous, source=source, environment=environment, account=account, is_ancestor=is_ancestor
            ):
                decision["reason"] = "Prior connector group needs independent readback before recovery or reopening"
            else:
                decision["action"] = "protected-apply-required"
                decision["reason"] = "Observed release failed; coordinator may review recovery"
        elif source != main:
            decision["reason"] = "Main advanced without an observed compatible release; no older dispatch"
        elif step["trigger"].startswith("push"):
            decision["reason"] = "Automatic run unobserved; wait before recovery dispatch"
        else:
            decision["action"] = "protected-apply-required"
            decision["reason"] = "No automatic release for this protected dispatch path"
        decisions.append(decision)
    return {"status": "plan-only", "source_revision": source, "main_revision": main,
            "decisions": decisions}


def github_json(path, *, paginate=False):
    arguments = ["gh", "api"]
    if paginate:
        arguments += ["--paginate", "--jq", ".workflow_runs[] | {id,run_attempt,head_sha,status,conclusion,event,inputs} | @json"]
    arguments.append(path)
    output = subprocess.check_output(arguments, text=True)
    return [json.loads(line) for line in output.splitlines()] if paginate else json.loads(output)


def private_document(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 65536:
        raise ValueError("Private prior-release inputs must be bounded owner-only files")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Private prior-release inputs must be objects")
    return value


def private_prior_readbacks(directory):
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise ValueError("Prior runtime readbacks must come from a private observer directory")
    observed = {}
    for phase in ("before", "after"):
        report = private_document(directory / f"runtime-{phase}.json")
        try:
            timestamp = datetime.fromisoformat(report["observed_at"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Prior runtime readback timestamp is invalid") from None
        if (report.get("phase") != phase or not isinstance(report.get("runtime"), dict)
                or timestamp.tzinfo is None
                or not 0 <= (datetime.now(UTC) - timestamp).total_seconds() <= 300):
            raise ValueError("Prior runtime readback is stale or incomplete")
        observed[phase] = report["runtime"]
    return observed


def main():
    parser = argparse.ArgumentParser(description="Observe protected runs before proposing any recovery")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--receipts", required=True, type=Path)
    parser.add_argument("--environment", required=True, choices=("dev", "prod"))
    parser.add_argument("--account", required=True)
    parser.add_argument("--previous-manifest", type=Path)
    parser.add_argument("--previous-readbacks", type=Path)
    parser.add_argument("--previous-gateway-image")
    parser.add_argument("--previous-facade-image")
    arguments = parser.parse_args()
    previous_inputs = (arguments.previous_manifest, arguments.previous_readbacks,
                       arguments.previous_gateway_image, arguments.previous_facade_image)
    if any(value is not None for value in previous_inputs) and not all(value is not None for value in previous_inputs):
        parser.error("Supply all prior-release inputs together or omit them")
    previous = None
    if arguments.previous_manifest is not None:
        manifest = private_document(arguments.previous_manifest)
        readbacks = private_prior_readbacks(arguments.previous_readbacks)
        previous = {"manifest": manifest, "readback": lambda phase: readbacks[phase],
                    "gateway_image": arguments.previous_gateway_image,
                    "facade_image": arguments.previous_facade_image}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", arguments.repo):
        parser.error("Reviewed repository required")
    if not all(REVISION.fullmatch(value) for value in (arguments.base, arguments.source)):
        parser.error("Reviewed base and source commits required")
    repository = f"repos/{arguments.repo}"
    branch = github_json(repository)["default_branch"]
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", branch) or ".." in branch:
        parser.error("Unsupported default branch")
    main_revision = github_json(f"{repository}/branches/{branch}")["commit"]["sha"]

    def is_ancestor(older, newer):
        comparison = github_json(f"{repository}/compare/{older}...{newer}")
        return comparison.get("status") in ("identical", "ahead")

    if not is_ancestor(arguments.base, arguments.source):
        parser.error("Source must descend from the reviewed release base")
    paths = subprocess.check_output(["git", "-C", str(Path(__file__).resolve().parents[3]),
                                     "diff", "--name-only", arguments.base, arguments.source], text=True).splitlines()
    steps = operator_sequence(paths)
    observed = []
    for workflow in {step["workflow"] for step in steps if step["workflow"]}:
        for run in github_json(f"{repository}/actions/workflows/{workflow}/runs?branch={branch}&per_page=100",
                               paginate=True):
            observed.append({**run, "workflow": workflow})
    if github_json(f"{repository}/branches/{branch}")["commit"]["sha"] != main_revision:
        raise ValueError("Default branch moved during release observation; start again")
    result = reconcile(steps, arguments.source, main_revision,
                       json.loads(arguments.manifest.read_text()), observed,
                       json.loads(arguments.receipts.read_text()), is_ancestor,
                       environment=arguments.environment, account=arguments.account, previous=previous)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit("Coordinator: verify GitHub Actions read access for observed release runs") from None
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Release observation unavailable: {type(exc).__name__}") from None
