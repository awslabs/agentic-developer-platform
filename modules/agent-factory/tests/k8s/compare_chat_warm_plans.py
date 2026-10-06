"""Compare default factory plans with synthetic providers, never deployment state."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile


REPO = Path(__file__).resolve().parents[4]
INFRA = Path("modules/agent-factory/infra")
FIXTURE = Path(__file__).with_name("fixtures") / "chat_warm_disabled.tftest.hcl"


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def prepare(root):
    for pattern in ("*.tfvars", "*.tfvars.json", "*.auto.tfvars", "*.auto.tfvars.json"):
        for variables in root.glob(pattern):
            variables.unlink()
    tests = root / "warm-tests"
    tests.mkdir()
    shutil.copyfile(FIXTURE, tests / FIXTURE.name)
    bundle = root / ".build/session-sweeper/index.js"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text('throw new Error("Synthetic plan-only fixture; never deploy");\n')


def plan(root, environment, locked=False):
    init = ["terraform", f"-chdir={root}", "init", "-backend=false", "-input=false", "-no-color"]
    if locked:
        init.append("-lockfile=readonly")
    run(init, env=environment)
    output = run([
        "terraform", f"-chdir={root}", "test", "-test-directory=warm-tests",
        f"-filter=warm-tests/{FIXTURE.name}", "-json", "-verbose",
    ], env=environment)
    events = [json.loads(line) for line in output.splitlines()]
    summaries = [event["test_summary"] for event in events if event["type"] == "test_summary"]
    if len(summaries) != 1 or summaries[0]["status"] != "pass":
        raise RuntimeError("The isolated Terraform plan did not pass")
    plans = [event["test_plan"] for event in events if event["type"] == "test_plan"]
    if len(plans) != 1 or not plans[0]["resource_changes"]:
        raise RuntimeError("Expected one nonempty Terraform plan of the actual factory root")
    return {
        "resources": {resource["address"]: resource for resource in plans[0]["resource_changes"]},
        "outputs": plans[0]["output_changes"],
    }


def compare(before, after):
    changed = [
        f"{section}:{name}"
        for section in ("resources", "outputs")
        for name in sorted(before[section].keys() | after[section].keys())
        if before[section].get(name) != after[section].get(name)
    ]
    if changed:
        raise RuntimeError("Default plans differ: " + ", ".join(changed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="Explicit local base commit to compare")
    parser.add_argument("--plugin-cache", type=Path, help="Optional Terraform provider download cache")
    args = parser.parse_args()
    base = run(["git", "rev-parse", "--verify", f"{args.base}^{{commit}}"], cwd=REPO).strip()
    with tempfile.TemporaryDirectory(prefix="chat-warm-plan-") as temporary:
        directory = Path(temporary)
        archive = directory / "base.tar"
        run(["git", "archive", "--format=tar", f"--output={archive}", base, str(INFRA)], cwd=REPO)
        with tarfile.open(archive) as snapshot:
            snapshot.extractall(directory / "base", filter="data")
        before = directory / "base" / INFRA
        after = directory / "candidate" / INFRA
        shutil.copytree(REPO / INFRA, after, ignore=shutil.ignore_patterns(
            ".terraform", ".build", "*.tfstate*", "*.tfplan", "crash.log",
        ))
        home = directory / "home"
        home.mkdir()
        cache = args.plugin_cache.resolve() if args.plugin_cache else directory / "providers"
        cache.mkdir(parents=True, exist_ok=True)
        environment = {
            "PATH": os.environ["PATH"], "HOME": str(home), "CHECKPOINT_DISABLE": "1",
            "TF_IN_AUTOMATION": "1", "TF_CLI_CONFIG_FILE": os.devnull,
            "TF_PLUGIN_CACHE_DIR": str(cache), "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        }
        prepare(before)
        prepare(after)
        baseline = plan(before, environment)
        shutil.copyfile(before / ".terraform.lock.hcl", after / ".terraform.lock.hcl")
        candidate = plan(after, environment, locked=True)
        compare(baseline, candidate)
        print(f"PASS: base {base} and candidate working tree have identical default plans "
              f"({len(candidate['resources'])} resources; {len(candidate['outputs'])} outputs).")
        print("Synthetic providers/state/archive inputs only; no live drift, IAM, or rollout validation.")


if __name__ == "__main__":
    main()
