"""Exact checkpoint continuation and duplicate-tag manifest contracts; offline."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import claim_ecr_continuation as recovery
import diagnostic
from journal import Journal, ReconciliationRequired
import recovery_contract as contract
from test_diagnostic import MemoryS3


class ManifestTests(unittest.TestCase):
    def fixture(self):
        raw = json.dumps({"schemaVersion": 2, "layers": [{"digest": "sha256:layer"}]})
        digest = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        image = {
            "registryId": "879318057152",
            "repositoryName": "adp-arc-runner",
            "imageId": {"imageDigest": digest, "imageTag": "version"},
            "imageManifest": raw,
        }
        other = json.loads(json.dumps(image))
        other["imageId"]["imageTag"] = "latest"
        return {"images": [image, other], "failures": []}, digest

    def test_identical_manifests_under_two_tags_are_accepted(self):
        batch, digest = self.fixture()
        result = diagnostic.verified_ecr_manifest(
            batch, "879318057152", "adp-arc-runner", digest
        )
        self.assertEqual(result["schemaVersion"], 2)

    def test_each_duplicate_must_match_identity_and_content(self):
        for change in ["registry", "repository", "digest", "content", "failures"]:
            batch, digest = self.fixture()
            if change == "registry":
                batch["images"][1]["registryId"] = "000000000000"
            elif change == "repository":
                batch["images"][1]["repositoryName"] = "other"
            elif change == "digest":
                batch["images"][1]["imageId"]["imageDigest"] = "sha256:wrong"
            elif change == "content":
                batch["images"][1]["imageManifest"] += " "
            else:
                batch["failures"] = [{"failureCode": "ImageNotFound"}]
            with self.assertRaises(AssertionError):
                diagnostic.verified_ecr_manifest(
                    batch, "879318057152", "adp-arc-runner", digest
                )

    def test_empty_response_is_rejected(self):
        with self.assertRaises(AssertionError):
            diagnostic.verified_ecr_manifest(
                {"images": [], "failures": []},
                "879318057152",
                "adp-arc-runner",
                "sha256:none",
            )


class VersionedMemoryS3(MemoryS3):
    def get_object(self, **request):
        response = super().get_object(**request)
        response["VersionId"] = self.objects[request["Key"]].get(
            "version", "original-version"
        )
        return response

    def put_object(self, **request):
        response = super().put_object(**request)
        self.objects[request["Key"]]["version"] = response["VersionId"]
        return response


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "receipt.json"
        self.cfg_path = Path(__file__).with_name("config.json")
        self.cfg = json.loads(self.cfg_path.read_text())
        self.new_run = "36109999999"
        self.new_sha = "b" * 40
        self.caller = "arn:aws:sts::879318057152:assumed-role/adp-dev-agent-runner-role/new-session"
        self.original = {
            "workflow_run_id": contract.ORIGIN_RUN,
            "workflow_sha": contract.ORIGIN_WORKFLOW_SHA,
            "config_sha256": contract.ORIGIN_CONFIG_SHA,
            "role_arn": contract.ROLE_ARN,
            "caller_arn": "arn:aws:sts::879318057152:assumed-role/adp-dev-agent-runner-role/original-session",
            "checks": json.loads(json.dumps(contract.ORIGIN_CHECKS)),
            "failure": dict(contract.ORIGIN_FAILURE),
            "negative_resource_results": {
                "fixture": {"passed": True, "actual": "AccessDenied"}
            },
        }
        self.body = (
            json.dumps(self.original, sort_keys=True, indent=2) + "\n"
        ).encode()
        self.s3 = VersionedMemoryS3()
        self.s3.objects[self.cfg["receipt_key"]] = {
            "body": self.body,
            "ETag": '"original-etag"',
            "version": "original-version",
        }
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for module in [contract, recovery]:
            self.stack.enter_context(
                patch.object(
                    module, "ORIGIN_JOURNAL_SHA", hashlib.sha256(self.body).hexdigest()
                )
            )
            self.stack.enter_context(
                patch.object(module, "ORIGIN_ETAG", '"original-etag"')
            )
            self.stack.enter_context(
                patch.object(module, "ORIGIN_VERSION", "original-version")
            )
        self.stack.enter_context(
            patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("Network forbidden"),
            )
        )
        self.stack.enter_context(
            patch.object(
                socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")
            )
        )

    def claim(self, path=None):
        recovery.claim(
            path or self.path,
            self.s3,
            self.cfg,
            self.new_run,
            self.new_sha,
            self.caller,
        )

    def test_claim_only_appends_context_preserving_all_origin_evidence(self):
        self.claim()
        result = json.loads(self.path.read_text())
        self.assertEqual(
            {k: v for k, v in result.items() if k != "continuation"}, self.original
        )
        self.assertEqual(result["continuation"]["workflow_run_id"], self.new_run)
        self.assertEqual(self.s3.uploads[-1]["IfMatch"], '"original-etag"')
        self.assertNotIn("IfNoneMatch", self.s3.uploads[-1])
        summary = json.loads(self.path.with_name("summary.json").read_text())
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["reconciled_origin_failure"], self.original["failure"])

    def test_second_runner_cannot_claim_again(self):
        self.claim()
        before = len(self.s3.uploads)
        with self.assertRaises(AssertionError):
            self.claim(self.path.with_name("another.json"))
        self.assertEqual(len(self.s3.uploads), before)

    def test_wrong_hash_etag_or_version_cannot_claim(self):
        for field, value in [
            ("body", self.body + b" "),
            ("ETag", '"changed"'),
            ("version", "new-version"),
        ]:
            obj = self.s3.objects[self.cfg["receipt_key"]]
            old = obj[field]
            obj[field] = value
            with self.assertRaises(AssertionError):
                self.claim()
            obj[field] = old
        self.assertFalse(self.s3.uploads)

    def test_even_reviewed_hash_cannot_admit_prior_side_effect_intents(self):
        for field in contract.NO_PRIOR_EFFECT_FIELDS:
            mutated = {**self.original, field: False}
            body = (json.dumps(mutated, sort_keys=True, indent=2) + "\n").encode()
            with patch.object(
                contract, "ORIGIN_JOURNAL_SHA", hashlib.sha256(body).hexdigest()
            ):
                with self.assertRaises(AssertionError):
                    contract.validate_failed_checkpoint(
                        body, '"original-etag"', "original-version"
                    )

    def test_wrong_original_run_failure_or_config_is_rejected(self):
        for field, value in [
            ("workflow_run_id", "other"),
            ("workflow_sha", "c" * 40),
            ("config_sha256", "wrong"),
            ("failure", {"stage": "bedrock", "exception": "TimeoutError"}),
        ]:
            mutated = {**self.original, field: value}
            body = json.dumps(mutated).encode()
            with patch.object(
                contract, "ORIGIN_JOURNAL_SHA", hashlib.sha256(body).hexdigest()
            ):
                with self.assertRaises(AssertionError):
                    contract.validate_failed_checkpoint(
                        body, '"original-etag"', "original-version"
                    )

    def test_competing_cas_writer_prevents_claim(self):
        original_put = self.s3.put_object

        def competing(**request):
            self.s3.objects[self.cfg["receipt_key"]]["ETag"] = '"competitor"'
            return original_put(**request)

        self.s3.put_object = competing
        with self.assertRaises(ReconciliationRequired):
            self.claim()
        self.assertNotIn(
            "continuation", json.loads(self.s3.objects[self.cfg["receipt_key"]]["body"])
        )

    def test_ambiguous_claim_cannot_be_replayed(self):
        self.s3.fail_next = True
        with self.assertRaises(ReconciliationRequired):
            self.claim()
        with self.assertRaises(AssertionError):
            self.claim(self.path.with_name("retry.json"))
        self.assertEqual(len(self.s3.uploads), 1)

    def test_runtime_preserves_checks_and_records_new_failure_separately(self):
        self.claim()
        owner = self

        class Runtime:
            def get_caller_identity(self):
                return {"Account": owner.cfg["account"], "Arn": owner.caller}

            def get_authorization_token(self):
                raise RuntimeError("Deliberate stop at resumed ECR")

            def __getattr__(self, operation):
                raise AssertionError("Completed probe replayed: " + operation)

        class Session:
            def __init__(self, **kwargs):
                pass

            def get_credentials(self):
                return SimpleNamespace(method="assume-role-with-web-identity")

            def client(self, name, config=None):
                return owner.s3 if name == "s3" else Runtime()

        env = {
            "GITHUB_RUN_ID": self.new_run,
            "GITHUB_SHA": self.new_sha,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        args = [
            "diagnostic",
            "--config",
            str(self.cfg_path),
            "--receipt",
            str(self.path),
            "--stage",
            "runtime",
            "--execute",
        ]
        with (
            patch.dict(os.environ, env),
            patch.object(sys, "argv", args),
            patch.object(diagnostic.boto3, "Session", Session),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                diagnostic.main()
        result = json.loads(self.path.read_text())
        self.assertEqual(result["checks"], self.original["checks"])
        self.assertEqual(result["caller_arn"], self.original["caller_arn"])
        self.assertEqual(result["failure"], self.original["failure"])
        self.assertEqual(result["workflow_run_id"], contract.ORIGIN_RUN)
        self.assertEqual(result["continuation"]["failure"]["stage"], "ecr")
        self.assertFalse(
            json.loads(self.path.with_name("summary.json").read_text())["complete"]
        )

    def test_ambiguous_terminal_continuation_commit_never_reports_complete(self):
        self.claim()
        journal = Journal(
            self.path,
            self.s3,
            self.cfg["source_bucket"],
            self.cfg["receipt_key"],
            {"workflow_run_id": contract.ORIGIN_RUN},
        )
        journal.result["runtime_complete"] = True
        journal.result["checks"]["codebuild_pr"] = True
        self.s3.fail_next = True
        with self.assertRaises(ReconciliationRequired):
            journal.save()
        self.assertFalse(journal.summary()["complete"])
        self.assertTrue(journal.summary()["reconciliation_required"])


if __name__ == "__main__":
    unittest.main()
