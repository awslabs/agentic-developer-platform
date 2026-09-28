"""Verify U6's actual CLI-only push -> backend deployment -> served bytes boundary.

This module only reads GitHub metadata/source and public CLI downloads. It never
runs downloaded scripts, dispatches workflows, or changes the target environment.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

REPOSITORY = "aws-e/adp"
CLI_ROOT = "modules/gateway/cli/"
ARTIFACTS = ("adp-superplane.py", "adp", "install.sh")
# This is the existing approved CI/release target, not authorization to deploy.
TARGETS = {
    "embark1/dev": {
        "origin": "https://d1g6cal2ts4iis.cloudfront.net",
        "account": "879318057152",
        "region": "us-east-1",
    }
}


class EvidenceError(RuntimeError):
    """The required boundary is absent, mismatched, or not established."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def settings(environment: dict[str, str]) -> dict:
    required = (
        "SUPERPLANE_LIVE_ENVIRONMENT",
        "SUPERPLANE_LIVE_CLI_MERGE_SHA",
        "SUPERPLANE_LIVE_CLI_RUN_ID",
        "SUPERPLANE_LIVE_EVIDENCE_FILE",
    )
    missing = [name for name in required if not environment.get(name)]
    require(
        not missing,
        "BLOCKED: missing explicit acceptance inputs: " + ", ".join(missing),
    )
    target = environment[required[0]]
    sha = environment[required[1]]
    run = environment[required[2]]
    require(
        target in TARGETS, "BLOCKED: target is not in the reviewed environment registry"
    )
    require(
        re.fullmatch(r"[0-9a-f]{40}", sha) is not None,
        "Expected an exact 40-hex merge commit",
    )
    require(
        re.fullmatch(r"[1-9][0-9]*", run) is not None,
        "Expected a positive GitHub deployment run ID",
    )
    return {
        "environment": target,
        "sha": sha,
        "run_id": int(run),
        "evidence_file": environment[required[3]],
        **TARGETS[target],
    }


class GitHub:
    def __call__(self, path: str) -> dict | list:
        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{REPOSITORY}/{path}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=40,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EvidenceError("BLOCKED: GitHub metadata could not be read") from exc
        # gh's diagnostic stream can contain auth/configuration details; never echo it.
        require(
            result.returncode == 0, "BLOCKED: GitHub metadata/source request failed"
        )
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeError) as exc:
            raise EvidenceError("GitHub returned invalid JSON") from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EvidenceError(
            "Served artifact redirected away from the selected endpoint"
        )


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.build_opener(NoRedirect).open(
            request, timeout=30
        ) as response:
            require(response.status == 200, "Served artifact did not return HTTP 200")
            content = response.read(2_000_001)
    except EvidenceError:
        raise
    except OSError as exc:
        raise EvidenceError("BLOCKED: served artifact could not be read") from exc
    require(
        len(content) <= 2_000_000,
        "Served artifact exceeds the acceptance download limit",
    )
    return content


def pages(github, path: str, key: str | None = None) -> list:
    """Fail closed at the API's file ceiling rather than ignore later changes."""
    collected = []
    for page in range(1, 31):
        data = github(f"{path}?per_page=100&page={page}")
        items = data[key] if key else data
        require(isinstance(items, list), "Malformed paginated GitHub evidence")
        collected.extend(items)
        if len(items) < 100:
            return collected
    raise EvidenceError(
        "GitHub evidence exceeds the pagination bound; completeness is unproven"
    )


def source(github, sha: str, name: str) -> bytes:
    data = github(f"contents/{CLI_ROOT}{name}?ref={sha}")
    require(
        data.get("encoding") == "base64",
        "Source content is unavailable at the selected commit",
    )
    try:
        return base64.b64decode(data["content"].replace("\n", ""), validate=True)
    except (KeyError, ValueError) as exc:
        raise EvidenceError("Malformed source content") from exc


def verify(config: dict, github, fetch=download) -> dict:
    sha, run_id = config["sha"], config["run_id"]
    run = github(f"actions/runs/{run_id}")
    require(
        run.get("id") == run_id,
        "Deployment run identity does not match the requested ID",
    )
    require(
        run.get("repository", {}).get("full_name") == REPOSITORY,
        "Deployment belongs to another repository",
    )
    require(
        run.get("path") == ".github/workflows/gateway-deploy.yml",
        "Run is not the gateway deployment workflow",
    )
    require(
        run.get("event") == "push" and run.get("head_branch") == "main",
        "Run must be a main push, not a manual dispatch",
    )
    require(
        run.get("head_sha") == sha,
        "Deployment run does not match the selected merge commit",
    )
    require(
        run.get("status") == "completed" and run.get("conclusion") == "success",
        "Deployment run has not completed successfully",
    )
    attempt = run.get("run_attempt")
    require(type(attempt) is int and attempt > 0, "Deployment attempt is missing")
    jobs = pages(github, f"actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    backend = [j for j in jobs if j.get("name") == "Build and Deploy Backend"]
    require(
        len(backend) == 1
        and backend[0].get("conclusion") == "success"
        and backend[0].get("status") == "completed"
        and backend[0].get("run_id") == run_id,
        "Backend deployment was absent, skipped, or unsuccessful",
    )

    commit = github(f"commits/{sha}")
    require(commit.get("sha") == sha, "GitHub commit identity changed")
    parents = commit.get("parents", [])
    require(bool(parents), "Cannot establish the merge's first-parent diff")
    files = pages(github, f"commits/{sha}", "files")
    require(bool(files), "Selected merge contains no changed files")
    require(
        all(
            f.get("filename", "").startswith(CLI_ROOT)
            and f.get("previous_filename", CLI_ROOT).startswith(CLI_ROOT)
            for f in files
        ),
        "Merge includes non-CLI changes; it cannot prove CLI-only triggering",
    )
    extension = [f for f in files if f.get("filename") == CLI_ROOT + ARTIFACTS[0]]
    require(
        len(extension) == 1 and extension[0].get("status") in {"added", "modified"},
        "Choose a CLI-only merge that changes the served Superplane extension",
    )
    pulls = pages(github, f"commits/{sha}/pulls")
    merged = [
        p
        for p in pulls
        if p.get("merged_at")
        and p.get("merge_commit_sha") == sha
        and p.get("base", {}).get("ref") == "main"
        and p.get("base", {}).get("repo", {}).get("full_name") == REPOSITORY
    ]
    require(
        len(merged) == 1,
        "Selected commit has no unique merged PR targeting this repository's main",
    )

    extension_source = source(github, sha, ARTIFACTS[0])
    if extension[0]["status"] == "modified":
        require(
            extension_source != source(github, parents[0]["sha"], ARTIFACTS[0]),
            "Extension bytes did not change; an old served artifact would be indistinguishable",
        )
    observed = datetime.now(timezone.utc).isoformat()
    records = []
    for name in ARTIFACTS:
        expected = (
            extension_source if name == ARTIFACTS[0] else source(github, sha, name)
        )
        require(bool(expected), "Selected source artifact is empty")
        url = (
            config["origin"]
            + "/api/cli/"
            + name
            + "?"
            + urlencode({"acceptance_sha": sha, "observed_at": observed})
        )
        actual = fetch(url)
        require(actual == expected, f"Served {name} does not match the selected merge")
        records.append(
            {
                "name": name,
                "url": url,
                "http_status": 200,
                "sha256": hashlib.sha256(actual).hexdigest(),
                "bytes": len(actual),
            }
        )
    # Reject a re-run/head change while the evidence was being gathered.
    after = github(f"actions/runs/{run_id}")
    require(
        all(
            after.get(k) == run.get(k)
            for k in ("head_sha", "run_attempt", "status", "conclusion")
        ),
        "Deployment evidence changed during verification; repeat against a stable run",
    )
    live = type(github) is GitHub and fetch is download
    return {
        "criterion": "U6-L1 / R16 acceptance 1 delivery",
        "evidence_kind": "live" if live else "offline-fixture",
        "result": "passed" if live else "matched",
        "observed_at": observed,
        "environment": config["environment"],
        "target": {k: config[k] for k in ("origin", "account", "region")},
        "repository": REPOSITORY,
        "merge_sha": sha,
        "first_parent": parents[0]["sha"],
        "pr_url": merged[0]["html_url"],
        "run_url": run["html_url"],
        "run_attempt": attempt,
        "backend_job_id": backend[0]["id"],
        "changed_files": [
            {k: f[k] for k in ("filename", "previous_filename", "status") if k in f}
            for f in files
        ],
        "artifacts": records,
        "scope": "Read-only CLI delivery observation; no deployment performed and no other live acceptance implied",
    }
