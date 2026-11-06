"""Single ingestion or DeepWiki builds require guarded image promotion."""

import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


BUILD_JOBS = {
    "Build ingestion", "Build codegraph-context", "Build litellm-proxy",
    "Build deepwiki", "Build context-mcp",
}


def requires_full_deployment(payload):
    jobs = payload["jobs"]
    if payload["total_count"] != len(jobs):
        raise ValueError("Incomplete build job inventory")
    builds = set()
    for job in jobs:
        name = job["name"]
        if name == "Detect Changed Images":
            continue
        if name not in BUILD_JOBS:
            raise ValueError("Unknown build job; select deployment explicitly")
        if job["status"] != "completed" or job["conclusion"] != "success":
            raise ValueError("Build job did not succeed")
        builds.add(name)
    return bool(builds) and builds not in ({"Build ingestion"}, {"Build deepwiki"})


def main():
    if os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch":
        full = True
    else:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        run = event["workflow_run"]
        if (
            run["head_branch"] != "main"
            or run["head_repository"]["full_name"] != os.environ["GITHUB_REPOSITORY"]
            or run["path"] != ".github/workflows/agent-context-images-build.yml"
            or run["conclusion"] != "success"
        ):
            raise ValueError("Not a successful main image build")
        run_id, attempt = int(run["id"]), int(run["run_attempt"])
        if min(run_id, attempt) < 1:
            raise ValueError("Invalid build run identity")
        url = (
            f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100"
        )
        request = Request(url, headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
        })
        with urlopen(request, timeout=20) as response:
            full = requires_full_deployment(json.load(response))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"full_deployment={str(full).lower()}\n")
    if not full:
        print("Use ingestion_only or deepwiki_only with exact digests for scoped image promotion.")


if __name__ == "__main__":
    main()
