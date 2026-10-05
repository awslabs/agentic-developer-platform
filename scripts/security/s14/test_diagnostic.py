"""Offline failure/replay and exact-build acceptance contracts. No AWS calls."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import zipfile

from botocore.exceptions import ClientError
import diagnostic
from journal import Journal, ReconciliationRequired

BASE = Path(__file__).parent


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.fail_next = False

    def put_object(self, **request):
        key = request["Key"]
        prior = self.objects.get(key)
        if request.get("IfNoneMatch") == "*" and prior:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if "IfMatch" in request and (not prior or request["IfMatch"] != prior["ETag"]):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        body = request["Body"]
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        self.objects[key] = {"body": body, "ETag": etag}
        self.uploads.append(request)
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("Synthetic ambiguous response after persistence")
        return {"ETag": etag, "VersionId": "version-" + str(len(self.uploads))}

    def get_object(self, **request):
        if request["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
        item = self.objects[request["Key"]]
        return {"Body": io.BytesIO(item["body"]), "ETag": item["ETag"]}


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()

        def git(*args):
            return subprocess.check_output(
                ["git", *args], cwd=self.source, text=True, stderr=subprocess.DEVNULL
            ).strip()

        git("init")
        (self.source / ".gitignore").write_text("ignored.env\n")
        (self.source / "tracked.txt").write_text("reviewed source")
        git("add", ".")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "fixture",
        )
        self.sha = git("rev-parse", "HEAD")
        (self.source / "ignored.env").write_text("PRIVATE-IGNORED-SENTINEL")
        (self.source / "untracked.txt").write_text("PRIVATE-UNTRACKED-SENTINEL")
        self.cfg = json.loads((BASE / "config.json").read_text())
        self.cfg["reviewed_source_sha"] = self.sha
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps(self.cfg))
        self.config_hash = hashlib.sha256(self.config.read_bytes()).hexdigest()
        self.receipt = self.root / "receipt.json"
        self.s3 = MemoryS3()
        self.calls = []
        self.secrets = []
        self.ambiguous = False
        self.negative_error = None
        self.identity_role = self.cfg["role_name"]
        self.build = None
        self.bedrock_calls = 0
        self.log_calls = 0
        owner = self

        class Session:
            def __init__(self, **kwargs):
                pass

            def get_credentials(self):
                return SimpleNamespace(method="assume-role-with-web-identity")

            def client(self, service, config=None):
                if (
                    service in ("bedrock-runtime", "codebuild")
                    and config.retries.get("total_max_attempts") == 1
                ):
                    owner.single_attempt = True
                return owner.s3 if service == "s3" else owner

        self.enter = contextlib.ExitStack()
        self.addCleanup(self.enter.close)
        self.enter.enter_context(patch.object(diagnostic.boto3, "Session", Session))
        self.enter.enter_context(
            patch.object(diagnostic, "APPROVED_CONFIG_SHA256", self.config_hash)
        )
        self.enter.enter_context(
            patch.dict(
                os.environ, {"GITHUB_RUN_ID": "12345678901", "GITHUB_SHA": "a" * 40}
            )
        )
        self.enter.enter_context(
            patch.object(
                socket.socket,
                "connect",
                side_effect=AssertionError("Network forbidden"),
            )
        )
        self.enter.enter_context(
            patch.object(
                socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")
            )
        )

    def get_caller_identity(self):
        return {
            "Account": self.cfg["account"],
            "Arn": f"arn:aws:sts::{self.cfg['account']}:assumed-role/{self.identity_role}/session",
        }

    def seed(self, omit=()):
        result = {
            "checks": {
                k: True
                for k in [
                    "ssm",
                    "secret_kms",
                    "negative_resources",
                    "ecr_pull",
                    "own_log",
                    "gateway_route",
                    "bedrock",
                ]
                if k not in omit
            },
            "config_sha256": self.config_hash,
            "role_arn": self.cfg["role_arn"],
            "workflow_run_id": "12345678901",
            "workflow_sha": "a" * 40,
            "runtime_complete": not omit,
        }
        journal = Journal(
            self.receipt,
            self.s3,
            self.cfg["source_bucket"],
            self.cfg["receipt_key"],
            result,
        )
        return journal

    def invoke(self, stage):
        args = [
            "diagnostic",
            "--config",
            str(self.config),
            "--receipt",
            str(self.receipt),
            "--stage",
            stage,
            "--execute",
            "--source-root",
            str(self.source),
            "--source-sha",
            self.sha,
        ]
        output = io.StringIO()
        fds = []
        real_flock = diagnostic.fcntl.flock

        def flock(fd, operation):
            fds.append(fd)
            return real_flock(fd, operation)

        with (
            patch.object(sys, "argv", args),
            patch.object(diagnostic.fcntl, "flock", flock),
            contextlib.redirect_stdout(output),
        ):
            try:
                diagnostic.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
            except Exception:
                code = 1
            finally:
                for fd in fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        self.assertNotIn("PRIVATE-", output.getvalue())
        return code

    def start_build(self, **request):
        self.calls.append(request)
        persisted = json.loads(self.s3.objects[self.cfg["receipt_key"]]["body"])
        self.assertTrue(persisted["build_invocation_started"])
        self.assertEqual(persisted["build_request"], request)
        if self.ambiguous:
            raise TimeoutError("Unknown accepted state")
        self.build = {
            "id": "build:123",
            "buildStatus": "SUCCEEDED",
            "serviceRole": request["serviceRoleOverride"],
            "projectName": request["projectName"],
            "sourceVersion": request["sourceVersion"],
            "source": {
                "type": "S3",
                "location": request["sourceLocationOverride"],
                "buildspec": request["buildspecOverride"],
            },
            "environment": {
                "environmentVariables": request["environmentVariablesOverride"]
            },
        }
        return {"build": {"id": "build:123", "buildStatus": "IN_PROGRESS"}}

    def batch_get_builds(self, **request):
        self.assertEqual(request["ids"], ["build:123"])
        return {"builds": [self.build]}

    def get_parameter(self, **request):
        raise ClientError(
            {"Error": {"Code": self.negative_error or "AccessDeniedException"}},
            "GetParameter",
        )

    def get_secret_value(self, **request):
        if request["SecretId"] in self.cfg["secrets"]:
            self.secrets.append(request["SecretId"])
            return {"SecretString": "PRIVATE-SECRET-SENTINEL"}
        raise ClientError(
            {"Error": {"Code": self.negative_error or "AccessDeniedException"}},
            "GetSecretValue",
        )

    class exceptions:
        class ResourceAlreadyExistsException(Exception):
            pass

    def create_log_stream(self, **request):
        persisted = json.loads(self.s3.objects[self.cfg["receipt_key"]]["body"])
        self.assertEqual(persisted["log_stream"], request["logStreamName"])

    def put_log_events(self, **request):
        self.log_calls += 1
        persisted = json.loads(self.s3.objects[self.cfg["receipt_key"]]["body"])
        self.assertTrue(persisted["log_event_started"])
        self.assertEqual(len(request["logEvents"]), 1)
        raise TimeoutError("Unknown log response")

    def invoke_model(self, **request):
        self.bedrock_calls += 1
        persisted = json.loads(self.s3.objects[self.cfg["receipt_key"]]["body"])
        self.assertTrue(persisted["model_invocation_started"])
        self.assertEqual(json.loads(request["body"])["max_tokens"], 1)
        raise TimeoutError("Unknown model response")

    def test_source_archive_version_and_complete_build_contract(self):
        self.seed()
        self.assertEqual(self.invoke("start-build"), 0)
        archive = next(x for x in self.s3.uploads if x["Key"].endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(archive["Body"])) as z:
            self.assertEqual(set(z.namelist()), {".gitignore", "tracked.txt"})
        self.assertTrue(self.calls[0]["sourceVersion"].startswith("version-"))
        self.assertEqual(self.invoke("poll-build"), 0)
        self.assertTrue(
            json.loads(self.receipt.with_name("summary.json").read_text())["complete"]
        )
        self.assertEqual(self.invoke("start-build"), 1)
        self.assertEqual(len(self.calls), 1)

    def test_ambiguous_start_never_replays(self):
        self.seed()
        self.ambiguous = True
        self.assertEqual(self.invoke("start-build"), 1)
        original = json.loads(self.receipt.read_text())["build_request"]
        self.assertEqual(self.invoke("start-build"), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            original, json.loads(self.receipt.read_text())["build_request"]
        )
        self.assertTrue(self.single_attempt)

    def test_mismatched_archive_or_build_cannot_pass(self):
        self.seed()
        self.assertEqual(self.invoke("start-build"), 0)
        original = json.loads(json.dumps(self.build))
        variants = [
            ("source", "location"),
            ("source", "buildspec"),
            (None, "sourceVersion"),
            (None, "serviceRole"),
            (None, "id"),
        ]
        for nested, key in variants:
            self.build = json.loads(json.dumps(original))
            (self.build[nested] if nested else self.build)[key] = "WRONG"
            self.assertEqual(self.invoke("poll-build"), 1)
            self.assertFalse(
                json.loads(self.receipt.with_name("summary.json").read_text())[
                    "complete"
                ]
            )
        self.build = json.loads(json.dumps(original))
        self.build["environment"]["environmentVariables"][0]["value"] = "WRONG"
        self.assertEqual(self.invoke("poll-build"), 1)

    def test_uncertain_terminal_commit_never_reports_complete(self):
        self.seed()
        self.assertEqual(self.invoke("start-build"), 0)
        original = self.s3.put_object
        for persisted in (False, True):

            def uncertain(**request):
                payload = json.loads(request["Body"])
                if payload.get("checks", {}).get("codebuild_pr"):
                    if persisted:
                        original(**request)
                    raise TimeoutError("Terminal commit response uncertain")
                return original(**request)

            self.s3.put_object = uncertain
            self.assertEqual(self.invoke("poll-build"), 1)
            summary = json.loads(self.receipt.with_name("summary.json").read_text())
            self.assertFalse(summary["complete"])
            self.assertTrue(summary["reconciliation_required"])
            # Reconcile test fixtures only, never production recovery behavior.
            body = self.s3.objects[self.cfg["receipt_key"]]["body"]
            self.receipt.write_bytes(body)
            self.s3.put_object = original

    def test_terminal_build_failures_do_not_pass(self):
        self.seed()
        self.assertEqual(self.invoke("start-build"), 0)
        for status in ["FAILED", "FAULT", "STOPPED", "TIMED_OUT"]:
            self.build["buildStatus"] = status
            self.assertEqual(self.invoke("poll-build"), 1)
            self.assertFalse(
                json.loads(self.receipt.with_name("summary.json").read_text())[
                    "complete"
                ]
            )

    def test_six_secret_reads_stay_out_of_outputs_and_receipts(self):
        self.seed(omit=["secret_kms"])
        self.assertEqual(self.invoke("runtime"), 0)
        self.assertEqual(self.secrets, self.cfg["secrets"])
        self.assertNotIn("PRIVATE-", self.receipt.read_text())
        self.assertNotIn(
            "PRIVATE-", self.s3.objects[self.cfg["receipt_key"]]["body"].decode()
        )

    def test_only_access_denied_satisfies_negative_reads(self):
        self.seed(omit=["negative_resources"])
        self.assertEqual(self.invoke("runtime"), 0)
        self.assertEqual(
            len(json.loads(self.receipt.read_text())["negative_resource_results"]), 3
        )

    def test_resource_missing_does_not_satisfy_negative_read(self):
        self.seed(omit=["negative_resources"])
        self.negative_error = "ResourceNotFoundException"
        self.assertEqual(self.invoke("runtime"), 1)
        self.assertFalse(json.loads(self.receipt.read_text()).get("runtime_complete"))

    def test_model_intent_is_remote_before_call_and_never_replayed(self):
        self.seed(omit=["bedrock"])
        self.assertEqual(self.invoke("runtime"), 1)
        self.assertEqual(self.invoke("runtime"), 1)
        self.assertEqual(self.bedrock_calls, 1)

    def test_log_intent_is_remote_before_call_and_never_replayed(self):
        self.seed(omit=["own_log"])
        self.assertEqual(self.invoke("runtime"), 1)
        self.assertEqual(self.invoke("runtime"), 1)
        self.assertEqual(self.log_calls, 1)

    def test_build_requires_completed_runtime(self):
        self.seed(omit=["bedrock"])
        self.assertEqual(self.invoke("start-build"), 1)
        self.assertFalse(self.calls)

    def test_unversioned_source_cannot_start_build(self):
        self.seed()
        original = self.s3.put_object

        def unversioned(**request):
            response = original(**request)
            if request["Key"].endswith(".zip"):
                response.pop("VersionId")
            return response

        self.s3.put_object = unversioned
        self.assertEqual(self.invoke("start-build"), 1)
        self.assertFalse(self.calls)

    def test_wrong_role_rejected_before_journal_or_probes(self):
        self.identity_role = "ec2-admin"
        self.assertEqual(self.invoke("runtime"), 1)
        self.assertFalse(self.s3.uploads)
        self.assertFalse(self.secrets)

    def test_lost_pod_cannot_start_fresh_attempt(self):
        self.seed()
        self.receipt.unlink()
        self.assertEqual(self.invoke("runtime"), 1)
        self.assertFalse(self.calls)

    def test_remote_write_ambiguity_stops_before_build(self):
        self.seed()
        self.s3.fail_next = True
        self.assertEqual(self.invoke("start-build"), 1)
        self.assertFalse(self.calls)


class JournalTests(unittest.TestCase):
    def test_uncertain_commit_cannot_be_retried_by_error_handler(self):
        with tempfile.TemporaryDirectory() as td:
            s3 = MemoryS3()
            path = Path(td) / "receipt.json"
            journal = Journal(path, s3, "bucket", "key", {"checks": {}})
            journal.result["model_invocation_started"] = True
            s3.fail_next = True
            with self.assertRaises(ReconciliationRequired):
                journal.save()
            count = len(s3.uploads)
            with self.assertRaises(ReconciliationRequired):
                journal.save()
            self.assertEqual(len(s3.uploads), count)
            self.assertTrue(
                json.loads(s3.objects["key"]["body"])["model_invocation_started"]
            )

    def test_conditional_write_rejects_competing_writer(self):
        with tempfile.TemporaryDirectory() as td:
            s3 = MemoryS3()
            path = Path(td) / "receipt.json"
            journal = Journal(path, s3, "bucket", "key", {"checks": {}})
            s3.put_object(Bucket="bucket", Key="key", Body=b'{"other":"writer"}')
            with self.assertRaises(ReconciliationRequired):
                journal.save()
            with self.assertRaises(ReconciliationRequired):
                Journal(path, s3, "bucket", "key", {"checks": {}})


if __name__ == "__main__":
    unittest.main()
