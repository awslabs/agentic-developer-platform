"""Terminal lineage and atomic expected-head deletion of an owned branch."""

import os
from pathlib import Path
import re
import subprocess
import tempfile

from tests.e2e.orchestration.config import resolve_secret_ref
from .http import Unsupported


def terminal_flow(client, flow_id, qualification_id):
    graph = client.get(f"/orchestration/flows/{flow_id}")
    if qualification_from_slug(graph["slug"]) != qualification_id:
        raise Unsupported("cleanup flow ownership changed")
    runs = []
    for node in graph["nodes"]:
        if node["kind"] == "gate":
            continue
        history = node.get("execution_history") or {}
        if not history.get("history_complete"):
            raise Unsupported("terminal worker history is unavailable")
        if node["state"] not in {
            "passed",
            "failed",
            "halted",
            "rejected_at_gate",
            "superseded",
        }:
            raise Unsupported("flow can still dispatch work; cleanup refused")
        for run in history["runs"]:
            invocation = client.get("/me/agent-invocations/" + run["invocation_id"])
            if (
                invocation["repo"] != client.config.repository
                or invocation["liveness"] != "exited"
            ):
                raise Unsupported("worker termination is not proven")
            runs.append(
                invocation["invocation_id"]
                if "invocation_id" in invocation
                else run["invocation_id"]
            )
    return graph, runs


def delete_branch(client, branch, expected_sha):
    if not re.fullmatch(
        r"qualification/q-[a-z0-9-]{8,50}/story-[12]", branch
    ) or not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise Unsupported("invalid owned branch or expected SHA")
    path = f"/repos/{client.config.repository}/git/ref/heads/{branch}"
    status, result = client.request("GET", path, github=True)
    if status == 404:
        return
    if status != 200 or result.get("object", {}).get("sha") != expected_sha:
        raise Unsupported("branch head changed before atomic cleanup")
    reference = client.config.secret_refs.get("github")
    if not reference:
        raise Unsupported("scoped GitHub credential unavailable")
    # Git receive-pack implements the expected old SHA atomically. The REST
    # delete-ref endpoint cannot express it. This does not force-update code.
    with tempfile.TemporaryDirectory(prefix="q2-branch-cleanup-") as directory:
        askpass = Path(directory) / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) printf "%s" x-access-token;; *) printf "%s" "$Q2_GITHUB_CREDENTIAL";; esac\n'
        )
        askpass.chmod(0o700)
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"}
        }
        env.update(
            GIT_ASKPASS=str(askpass),
            GIT_TERMINAL_PROMPT="0",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null",
            Q2_GITHUB_CREDENTIAL=resolve_secret_ref(reference),
        )
        try:
            subprocess.run(
                ["git", "init", "--bare", directory + "/repo"],
                env=env,
                capture_output=True,
                check=True,
                timeout=10,
            )
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    directory + "/repo",
                    "-c",
                    "credential.helper=",
                    "push",
                    "--porcelain",
                    f"--force-with-lease=refs/heads/{branch}:{expected_sha}",
                    "https://github.com/" + client.config.repository + ".git",
                    ":refs/heads/" + branch,
                ],
                env=env,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            raise Unsupported(
                "branch cleanup outcome unknown; reconcile before another attempt"
            ) from None
        if result.returncode:
            raise Unsupported(
                "atomic branch deletion refused or unverified; retained expected SHA"
            )

    status, _ = client.request("GET", path, github=True)
    if status != 404:
        raise Unsupported("branch deletion was not verified; retain inventory")


def find_flow(client, qualification_id, suffix="", *, allow_absent=False):
    """Bounded exact-slug resolution; search hits alone never prove ownership."""
    from urllib.parse import quote

    slug = qualification_id + suffix
    matches = []
    offset = 0
    while offset < 1000:
        page = client.get(
            f"/orchestration/flows?q={quote(slug)}&limit=100&offset={offset}"
        )
        matches.extend(row for row in page["flows"] if row["slug"] == slug)
        offset += len(page["flows"])
        if offset >= page["total"]:
            break
        if not page["flows"]:
            raise Unsupported("incomplete flow discovery")
    else:
        raise Unsupported("flow discovery overflow")
    if not matches and allow_absent:
        return None
    if len(matches) != 1:
        raise Unsupported("flow discovery absent or ambiguous")
    return matches[0]["id"]


def qualification_from_slug(slug):
    for suffix in ("-refusal", "-allowance", "-revocation", "-stop"):
        if slug.endswith(suffix):
            slug = slug.removesuffix(suffix)
            break
    if not re.fullmatch(r"q-[a-z0-9-]{8,50}", slug):
        raise Unsupported("invalid qualification flow slug")
    return slug
