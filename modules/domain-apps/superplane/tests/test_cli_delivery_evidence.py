"""Offline regressions for U6's real-boundary verifier; never live evidence."""

from __future__ import annotations

import base64
import copy
import os
import subprocess
import sys
from pathlib import Path

import pytest

from superplane_acceptance import cli_delivery as delivery

SHA = "a" * 40
PARENT = "b" * 40
RUN_ID = 101


def environment():
    return {
        "SUPERPLANE_LIVE_ENVIRONMENT": "embark1/dev",
        "SUPERPLANE_LIVE_CLI_MERGE_SHA": SHA,
        "SUPERPLANE_LIVE_CLI_RUN_ID": str(RUN_ID),
        "SUPERPLANE_LIVE_EVIDENCE_FILE": "/unused/offline-evidence.json",
    }


class Boundary:
    """Fake transport, explicitly confined to these offline tests."""

    def __init__(self):
        self.run = {
            "id": RUN_ID,
            "repository": {"full_name": delivery.REPOSITORY},
            "path": ".github/workflows/gateway-deploy.yml",
            "head_sha": SHA,
            "event": "push",
            "head_branch": "main",
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
            "html_url": f"https://github.com/aws-e/adp/actions/runs/{RUN_ID}",
        }
        self.job = {
            "id": 42,
            "name": "Build and Deploy Backend",
            "status": "completed",
            "conclusion": "success",
            "run_id": RUN_ID,
        }
        self.files = [
            {
                "filename": delivery.CLI_ROOT + "adp-superplane.py",
                "status": "modified",
                "patch": "DO NOT COPY SOURCE PATCHES INTO EVIDENCE",
            }
        ]
        self.pull = {
            "number": 10,
            "merged_at": "2026-09-17T00:00:00Z",
            "merge_commit_sha": SHA,
            "base": {"ref": "main", "repo": {"full_name": delivery.REPOSITORY}},
            "html_url": "https://github.com/aws-e/adp/pull/10",
        }
        self.served = {name: ("new " + name).encode() for name in delivery.ARTIFACTS}
        self.reads = []
        self.run_reads = 0
        self.after = None

    def github(self, path):
        if path == f"actions/runs/{RUN_ID}":
            self.run_reads += 1
            return copy.deepcopy(
                self.after if self.run_reads > 1 and self.after else self.run
            )
        if path.startswith(f"actions/runs/{RUN_ID}/attempts/"):
            return {"jobs": [copy.deepcopy(self.job)]}
        if path == f"commits/{SHA}":
            return {"sha": SHA, "parents": [{"sha": PARENT}]}
        if path.startswith(f"commits/{SHA}/pulls?"):
            return [copy.deepcopy(self.pull)]
        if path.startswith(f"commits/{SHA}?"):
            page = int(path.rsplit("=", 1)[1])
            return {
                "sha": SHA,
                "files": copy.deepcopy(self.files[(page - 1) * 100 : page * 100]),
            }
        if path.startswith("contents/"):
            name = path.split("?")[0].rsplit("/", 1)[1]
            content = (
                "old " + name if path.endswith(PARENT) else "new " + name
            ).encode()
            return {"encoding": "base64", "content": base64.b64encode(content).decode()}
        raise AssertionError("Unexpected network path: " + path)

    def fetch(self, url):
        self.reads.append(url)
        name = url.split("?")[0].rsplit("/", 1)[1]
        return self.served[name]

    def verify(self):
        return delivery.verify(
            delivery.settings(environment()), self.github, self.fetch
        )


def test_complete_fixture_records_metadata_and_hashes_without_source_or_secrets():
    boundary = Boundary()
    result = boundary.verify()
    assert result["evidence_kind"] == "offline-fixture"
    assert result["result"] == "matched"
    assert result["merge_sha"] == SHA
    assert result["backend_job_id"] == 42
    assert len(result["artifacts"]) == 3
    assert all(
        x["http_status"] == 200 and len(x["sha256"]) == 64 for x in result["artifacts"]
    )
    assert all("acceptance_sha=" + SHA in url for url in boundary.reads)
    assert "DO NOT COPY" not in str(result)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", 102, "run identity"),
        ("repository", {"full_name": "someone/else"}, "another repository"),
        ("path", ".github/workflows/other.yml", "deployment workflow"),
        ("event", "workflow_dispatch", "main push"),
        ("head_branch", "topic", "main push"),
        ("head_sha", PARENT, "merge commit"),
        ("status", "in_progress", "completed successfully"),
        ("conclusion", "failure", "completed successfully"),
        ("run_attempt", None, "attempt is missing"),
    ],
)
def test_wrong_or_incomplete_run_is_rejected_before_live_download(
    field, value, message
):
    boundary = Boundary()
    boundary.run[field] = value
    with pytest.raises(delivery.EvidenceError, match=message):
        boundary.verify()
    assert not boundary.reads


@pytest.mark.parametrize("conclusion", ["skipped", "cancelled", "failure", None])
def test_overall_success_does_not_hide_skipped_or_failed_backend(conclusion):
    boundary = Boundary()
    boundary.job["conclusion"] = conclusion
    with pytest.raises(delivery.EvidenceError, match="Backend deployment"):
        boundary.verify()


def test_job_must_belong_to_the_requested_run():
    boundary = Boundary()
    boundary.job["run_id"] = 999
    with pytest.raises(delivery.EvidenceError, match="Backend deployment"):
        boundary.verify()


def test_later_diff_pages_cannot_hide_non_cli_changes():
    boundary = Boundary()
    boundary.files.extend(
        {"filename": delivery.CLI_ROOT + f"helper{i}.py", "status": "modified"}
        for i in range(99)
    )
    boundary.files.append(
        {"filename": "modules/gateway/src/main.py", "status": "modified"}
    )
    with pytest.raises(delivery.EvidenceError, match="non-CLI"):
        boundary.verify()


def test_rename_from_outside_cli_is_not_cli_only():
    boundary = Boundary()
    boundary.files[0]["previous_filename"] = "modules/gateway/src/main.py"
    with pytest.raises(delivery.EvidenceError, match="non-CLI"):
        boundary.verify()


def test_unchanged_extension_cannot_prove_new_bytes_were_delivered():
    boundary = Boundary()
    boundary.files = [
        {"filename": delivery.CLI_ROOT + "install.sh", "status": "modified"}
    ]
    with pytest.raises(
        delivery.EvidenceError, match="changes the served Superplane extension"
    ):
        boundary.verify()


def test_noop_byte_change_is_not_delivery_evidence():
    boundary = Boundary()
    github = boundary.github

    def unchanged(path):
        return github(path.replace("ref=" + PARENT, "ref=" + SHA))

    with pytest.raises(delivery.EvidenceError, match="bytes did not change"):
        delivery.verify(delivery.settings(environment()), unchanged, boundary.fetch)


@pytest.mark.parametrize(
    "field,value",
    [("merged_at", None), ("merge_commit_sha", PARENT), ("base", {"ref": "topic"})],
)
def test_non_merged_or_unrelated_pr_is_rejected(field, value):
    boundary = Boundary()
    boundary.pull[field] = value
    with pytest.raises(delivery.EvidenceError, match="unique merged PR"):
        boundary.verify()


@pytest.mark.parametrize("name", delivery.ARTIFACTS)
def test_every_served_component_must_match_the_same_selected_commit(name):
    boundary = Boundary()
    boundary.served[name] = b"stale or wrong content"
    with pytest.raises(delivery.EvidenceError, match="does not match"):
        boundary.verify()


def test_deployment_rerun_during_observation_invalidates_evidence():
    boundary = Boundary()
    boundary.after = {**boundary.run, "run_attempt": 2}
    with pytest.raises(delivery.EvidenceError, match="changed during verification"):
        boundary.verify()


def test_pagination_ceiling_fails_instead_of_accepting_an_incomplete_diff():
    with pytest.raises(delivery.EvidenceError, match="pagination bound"):
        delivery.pages(lambda path: [1] * 100, "commits/anything")


@pytest.mark.parametrize("key", list(environment()))
def test_missing_input_is_blocked(key):
    values = environment()
    del values[key]
    with pytest.raises(delivery.EvidenceError, match="BLOCKED"):
        delivery.settings(values)


def test_target_cannot_be_silently_redirected_to_another_environment():
    values = {**environment(), "SUPERPLANE_LIVE_ENVIRONMENT": "unapproved/prod"}
    with pytest.raises(delivery.EvidenceError, match="environment registry"):
        delivery.settings(values)


def test_live_entry_point_fails_without_inputs_instead_of_skipping(tmp_path):
    entry = Path(__file__).parent / "acceptance/test_u6_live.py"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUPERPLANE_LIVE_")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(entry), "-q", "-p", "no:cacheprovider"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "BLOCKED: missing explicit acceptance inputs" in result.stdout
    assert "1 failed" in result.stdout
    assert "skipped" not in result.stdout


def test_github_failure_does_not_echo_auth_diagnostics(monkeypatch):
    monkeypatch.setattr(
        delivery.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, b"", b"example-private-auth-diagnostic"
        ),
    )
    with pytest.raises(delivery.EvidenceError) as exc:
        delivery.GitHub()("actions/runs/101")
    assert "example-private" not in str(exc.value)


def test_redirected_download_is_rejected():
    with pytest.raises(delivery.EvidenceError, match="redirected"):
        delivery.NoRedirect().redirect_request(
            None, None, 302, "redirect", {}, "https://wrong.example/"
        )
