import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/insights/poc/devlake/scripts/verify_poc.py"
SHARED_CLI = "modules/gateway/src/cli_capabilities/contract.py"


class SharedCliPreflightTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("devlake_verify_poc", MODULE)
        self.verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.verifier)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / "candidate"
        self.repo.mkdir()
        self.remote = Path(temporary.name) / "origin.git"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "example@example.invalid")
        self.git("config", "user.name", "Offline Example")
        contract = self.repo / SHARED_CLI
        contract.parent.mkdir(parents=True)
        contract.write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base_before_predecessor = self.git("rev-parse", "HEAD")
        contract.write_text("security predecessor\n")
        self.git("add", ".")
        self.git("commit", "-qm", "reviewed predecessor")
        self.merge = self.git("rev-parse", "HEAD")
        (self.repo / "unrelated.txt").write_text("latest main\n")
        self.git("add", ".")
        self.git("commit", "-qm", "main advances after predecessor")
        self.main = self.git("rev-parse", "HEAD")
        subprocess.run(["git", "clone", "-q", "--bare", str(self.repo), str(self.remote)], check=True)
        self.git("remote", "add", "origin", str(self.remote))
        self.git("fetch", "-q", "origin", "main")
        self.git("checkout", "-qb", "candidate")
        contract.write_text("POC shared CLI update\n")
        self.git("add", ".")
        self.git("commit", "-qm", "POC candidate")
        self.head = self.git("rev-parse", "HEAD")
        self.candidate = {"number": 9179, "state": "open", "base": {"ref": "main", "sha": self.main},
                          "head": {"sha": self.head, "repo": {"full_name": "aws-e/adp"}}}
        self.predecessor = {"merged": True, "base": {"ref": "main"},
                            "head": {"sha": self.merge}, "merge_commit_sha": self.merge,
                            "user": {"login": "author"}}
        self.reviews = [{"state": "APPROVED", "commit_id": self.merge, "user": {"login": "reviewer"}}]
        self.runs = [self.check(name) for name in
                     ("L5 contract and module tests", "Lint", "Test", "Superplane / core")]
        self.requested = []
        self.patch_root = mock.patch.object(self.verifier, "ROOT", self.repo)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.patch_gh = mock.patch.object(self.verifier, "github", side_effect=self.github)
        self.patch_gh.start()
        self.addCleanup(self.patch_gh.stop)
        environment = mock.patch.dict(os.environ, {
            "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "aws-e/adp", "GITHUB_BASE_REF": "main",
            "ADP_DEVLAKE_PR_NUMBER": "9179", "ADP_DEVLAKE_PR_BASE_SHA": self.main,
            "ADP_DEVLAKE_PR_HEAD_SHA": self.head,
        })
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, *command):
        return subprocess.check_output(["git", *command], cwd=self.repo, text=True,
                                       stderr=subprocess.DEVNULL).strip()

    def check(self, name, conclusion="success"):
        return {"name": name, "status": "completed", "conclusion": conclusion,
                "head_sha": self.merge, "app": {"id": 15368}}

    def github(self, path):
        self.requested.append(path)
        return {
            "pulls/9179": self.candidate,
            "pulls/9132": self.predecessor,
            "pulls/9132/reviews?per_page=100": self.reviews,
            f"commits/{self.merge}/check-runs?per_page=100": {
                "total_count": len(self.runs), "check_runs": self.runs},
            "branches/main": {"commit": {"sha": self.main}},
        }[path]

    def test_verified_predecessor_checks_current_main_and_rebased_candidate(self):
        receipt = self.verifier.predecessor_preflight()
        self.assertEqual(receipt, {"shared_cli": "verified", "predecessor": self.merge,
                                   "candidate": self.head, "main": self.main})
        self.assertIn("pulls/9132", self.requested)
        self.assertEqual(self.requested[-1], "branches/main")

    def test_unrelated_or_manual_run_needs_no_cross_flow_gate(self):
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            self.assertEqual(self.verifier.predecessor_preflight(), {"shared_cli": "not-applicable"})
        self.git("checkout", "-q", "main")
        self.git("checkout", "-qb", "no-shared-change")
        (self.repo / "unrelated.txt").write_text("another unrelated change\n")
        self.git("add", ".")
        self.git("commit", "-qm", "unrelated candidate")
        with mock.patch.dict(os.environ, {"ADP_DEVLAKE_PR_HEAD_SHA": self.git("rev-parse", "HEAD")}):
            self.assertEqual(self.verifier.predecessor_preflight(), {"shared_cli": "unchanged"})
        self.assertEqual(self.requested, [])

    def test_moving_candidate_or_missing_context_refused_before_github(self):
        with mock.patch.dict(os.environ, {"ADP_DEVLAKE_PR_BASE_SHA": "main"}):
            with self.assertRaisesRegex(ValueError, "authenticated PR base"):
                self.verifier.predecessor_preflight()
        self.candidate["head"]["sha"] = "f" * 40
        with self.assertRaisesRegex(ValueError, "candidate PR changed"):
            self.verifier.predecessor_preflight()
        self.assertNotIn("pulls/9132", self.requested)

    def test_unmerged_or_unreviewed_predecessor_refused(self):
        self.predecessor["merged"] = False
        with self.assertRaisesRegex(ValueError, "not merged"):
            self.verifier.predecessor_preflight()
        self.predecessor["merged"] = True
        self.reviews[0]["commit_id"] = self.base_before_predecessor
        with self.assertRaisesRegex(ValueError, "independent approval"):
            self.verifier.predecessor_preflight()
        self.reviews[0]["commit_id"] = self.merge
        self.reviews[0]["user"]["login"] = "author"
        with self.assertRaisesRegex(ValueError, "independent approval"):
            self.verifier.predecessor_preflight()

    def test_failed_skipped_missing_or_unavailable_checks_refused(self):
        for conclusion in ("failure", "skipped", "cancelled"):
            with self.subTest(conclusion=conclusion):
                self.runs[0] = self.check("L5 contract and module tests", conclusion)
                with self.assertRaisesRegex(ValueError, "checks|required"):
                    self.verifier.predecessor_preflight()
        self.runs = [self.check("Lint"), self.check("Test")]
        with self.assertRaisesRegex(ValueError, "required"):
            self.verifier.predecessor_preflight()
        self.runs = [self.check("L5 contract and module tests"), self.check("Lint"),
                     self.check("Test", "failure")]
        with self.assertRaisesRegex(ValueError, "failed"):
            self.verifier.predecessor_preflight()
        with mock.patch.object(self.verifier, "github", side_effect=ValueError("evidence unavailable")):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                self.verifier.predecessor_preflight()

    def test_unmerged_ancestry_stale_main_and_unrebased_tree_refused(self):
        self.predecessor["merge_commit_sha"] = self.head
        with self.assertRaisesRegex(ValueError, "ancestry unavailable"):
            self.verifier.predecessor_preflight()
        self.predecessor["merge_commit_sha"] = self.merge
        self.main = self.head
        with self.assertRaisesRegex(ValueError, "not on current main"):
            self.verifier.predecessor_preflight()
        self.main = self.candidate["base"]["sha"]
        self.git("checkout", "-q", "main")
        with self.assertRaisesRegex(ValueError, "not on current main"):
            self.verifier.predecessor_preflight()
        self.git("checkout", "-qb", "stale", self.base_before_predecessor)
        (self.repo / SHARED_CLI).write_text("stale change\n")
        self.git("add", ".")
        self.git("commit", "-qm", "stale candidate")
        old_head = self.git("rev-parse", "HEAD")
        self.candidate["head"]["sha"] = old_head
        with mock.patch.dict(os.environ, {"ADP_DEVLAKE_PR_HEAD_SHA": old_head}):
            with self.assertRaisesRegex(ValueError, "ancestry unavailable"):
                self.verifier.predecessor_preflight()
