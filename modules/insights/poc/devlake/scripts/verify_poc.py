import hashlib
import importlib.util
import os
import re
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
CONNECTOR = "modules/connectors/devlake"
WORKER = "modules/insights/poc/devlake"
SHARED_CLI = "modules/gateway/src/cli_capabilities/contract.py"
REQUIRED_PREDECESSOR_CHECKS = {"L5 contract and module tests", "Lint", "Test"}
PREDECESSOR_PR = 9132
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def read(command, **kwargs):
    return subprocess.check_output(command, text=True, **kwargs).strip()


def github(path):
    try:
        return json.loads(subprocess.check_output(["gh", "api", "repos/aws-e/adp/" + path],
                                               text=True, stderr=subprocess.DEVNULL))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        raise ValueError("shared CLI predecessor GitHub evidence unavailable") from None


def git(*arguments):
    try:
        return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        raise ValueError("shared CLI candidate ancestry unavailable") from None


def predecessor_preflight():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return {"shared_cli": "not-applicable"}
    base = os.environ.get("ADP_DEVLAKE_PR_BASE_SHA", "")
    head = os.environ.get("ADP_DEVLAKE_PR_HEAD_SHA", "")
    number = os.environ.get("ADP_DEVLAKE_PR_NUMBER", "")
    if (os.environ.get("GITHUB_REPOSITORY") != "aws-e/adp" or
            not COMMIT.fullmatch(base) or not COMMIT.fullmatch(head) or
            not number.isdecimal() or int(number) <= 0 or
            os.environ.get("GITHUB_BASE_REF") != "main"):
        raise ValueError("authenticated PR base, head and repository are required")
    changed = git("diff", "--name-only", base + "..." + head, "--", SHARED_CLI)
    if not changed:
        return {"shared_cli": "unchanged"}
    if changed != SHARED_CLI:
        raise ValueError("shared CLI candidate diff is ambiguous")
    candidate = github("pulls/" + number)
    if (candidate["number"] != int(number) or candidate["state"] != "open" or
            candidate["head"]["sha"] != head or candidate["base"]["ref"] != "main" or
            candidate["base"]["sha"] != base or
            candidate["head"]["repo"]["full_name"] != "aws-e/adp"):
        raise ValueError("candidate PR changed during shared CLI preflight")
    predecessor = github("pulls/" + str(PREDECESSOR_PR))
    merge = predecessor.get("merge_commit_sha")
    source = predecessor["head"]["sha"]
    if (not predecessor.get("merged") or predecessor["base"]["ref"] != "main" or
            not COMMIT.fullmatch(merge or "") or not COMMIT.fullmatch(source)):
        raise ValueError("shared CLI predecessor is not merged into main")
    reviews = github("pulls/" + str(PREDECESSOR_PR) + "/reviews?per_page=100")
    if (len(reviews) == 100 or not any(review["state"] == "APPROVED" and
            review["commit_id"] == source and review["user"]["login"] != predecessor["user"]["login"]
            for review in reviews)):
        raise ValueError("shared CLI predecessor lacks a current independent approval")
    checks = github("commits/" + source + "/check-runs?per_page=100")
    runs = checks["check_runs"]
    if (checks["total_count"] != len(runs) or len(runs) >= 100 or not runs or
            any(run["head_sha"] != source or run["status"] != "completed" or
                run["conclusion"] not in ("success", "skipped") for run in runs)):
        raise ValueError("shared CLI predecessor checks are incomplete or failed")
    passed = {run["name"] for run in runs if run["conclusion"] == "success" and
              run["app"]["id"] == 15368}
    if (not REQUIRED_PREDECESSOR_CHECKS <= passed or
            any(run["name"] in REQUIRED_PREDECESSOR_CHECKS and run["conclusion"] != "success"
                for run in runs)):
        raise ValueError("required shared CLI predecessor checks are missing or not successful")
    remote = github("branches/main")["commit"]["sha"]
    if not COMMIT.fullmatch(remote):
        raise ValueError("current main revision is unavailable")
    if (git("rev-parse", "refs/remotes/origin/main") != remote or remote != base or
            git("merge-base", "--is-ancestor", merge, remote) or
            git("merge-base", "--is-ancestor", remote, head) or
            git("diff", "--name-only", "HEAD", head)):
        raise ValueError("shared CLI predecessor or candidate is not on current main")
    return {"shared_cli": "verified", "predecessor": merge, "candidate": head, "main": remote}


def verify():
    predecessor_preflight()
    revision = read(["git", "rev-parse", "HEAD"], cwd=ROOT)
    if read(["git", "status", "--porcelain", "--untracked-files=all", "--", CONNECTOR, WORKER], cwd=ROOT):
        raise ValueError("component sources must be committed before clean-workspace verification")

    results = {}
    with tempfile.TemporaryDirectory(prefix="devlake-scenario-") as temporary:
        for component in ("api", "worker"):
            workspace = Path(temporary) / component
            subprocess.run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(ROOT), str(workspace)],
                           check=True)
            subprocess.run(["git", "-C", str(workspace), "checkout", "--quiet", "--detach", revision], check=True)
            if read(["git", "-C", str(workspace), "status", "--porcelain", "--untracked-files=all"]):
                raise ValueError("scenario checkout is not clean")
            if read(["git", "-C", str(workspace), "rev-parse", "HEAD"]) != revision:
                raise ValueError("scenario checkout differs from pinned revision")

            layout = Path(temporary) / (component + ".oci")
            if component == "api":
                command = [str(workspace / CONNECTOR / "scripts/build.sh"),
                           "--customization", revision, "--component", "api", "--output", str(layout)]
            else:
                command = [str(workspace / WORKER / "scripts/build.sh"), revision, str(layout)]
            subprocess.run(command, cwd=workspace, stdout=subprocess.DEVNULL, check=True)

            spec = importlib.util.spec_from_file_location("devlake_scenario_" + component,
                                                          workspace / WORKER / "publish.py")
            publisher = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(publisher)
            index = json.loads((layout / "index.json").read_text())
            digest = index["manifests"][0]["digest"]
            schema_digest = publisher.inspect(layout, digest, revision, component)
            schema = json.loads((workspace / WORKER / "schema-manifest.json").read_text())
            lock = json.loads((workspace / CONNECTOR / "source-lock.json").read_text())
            contract_root = CONNECTOR if component == "api" else WORKER
            contract = json.loads((workspace / contract_root / "module-contract.json").read_text())
            if (schema["upstream_revision"] != lock["upstream"]["revision"] or
                    schema["raw"]["table_rule"] != "_raw_{table}" or
                    schema["raw"]["scope_rule"] != "_raw_{plugin}_scopes" or
                    schema["raw"]["allowed_plugins"] != ["github", "jira", "slack", "sonarqube", "jenkins", "argocd", "gitlab", "pagerduty", "linear"] or
                    schema["raw"]["raw_collector_runtime_ddl"] is not False or
                    schema["raw"]["patch"] not in [patch["path"] for patch in lock["patches"]] or
                    schema["schema_version"] != 2 or schema["staging"]["status"] != "ready" or
                    schema["migration_sql_sha256"] != hashlib.sha256(
                        (workspace / WORKER / "migrations/schema-v2.sql").read_bytes()).hexdigest() or
                    contract["default_enabled"] is not False or
                    schema_digest != hashlib.sha256((workspace / WORKER / "schema-manifest.json").read_bytes()).hexdigest()):
                raise ValueError("scenario schema or optional module differs from pinned source")
            results[component] = {"digest": digest, "schema_sha256": schema_digest,
                                  "upstream": lock["upstream"]["revision"], "platform": lock["platform"]}

    if results["api"]["digest"] == results["worker"]["digest"]:
        raise ValueError("API and worker must be independent image artifacts")
    return {"revision": revision, "images": results}


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--preflight-only"]:
            print(json.dumps(predecessor_preflight(), sort_keys=True))
        elif len(sys.argv) == 1:
            print(json.dumps(verify(), sort_keys=True))
        else:
            raise ValueError("only --preflight-only is supported")
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"DevLake scenario verification failed: {error}", file=sys.stderr)
        sys.exit(1)
