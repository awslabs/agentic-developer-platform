"""Exercise the pod's fixed helper entry point against mounted-home fixtures."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from crew_checkpoint import InvalidCheckpoint, capture
from crew_checkpoint_helper import run, run_restore


class Reply:
    def __init__(self, body=b"{}", headers=None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        return self.body[:size] if size >= 0 else self.body


class HelperTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "crew"
        self.home.mkdir()
        self.staged = self.home / "memory.db"
        with closing(sqlite3.connect(self.staged)) as db:
            db.execute("CREATE TABLE lessons (provenance TEXT)")
            db.execute("INSERT INTO lessons VALUES ('explicit')")
            db.commit()
        self.env = {
            "ADP_CHECKPOINT_ID": "a" * 36, "ADP_OWNER_KEY": "b" * 52,
            "ADP_CHAT_DATA_URL": "https://chat-sandbox-gateway.adp-gateway.svc:8443",
            "KIROCREW_HOME": "/mnt/crew", "ADP_WORKLOAD_TOKEN_FILE": "/var/run/adp-checkpoint/token",
            "NODE_EXTRA_CA_CERTS": "/var/run/adp-chat-ca/ca.crt",
        }
        self.requests = []

    def open(self, request, *, timeout, context):
        self.requests.append((request, context))
        if request.full_url.endswith("/upload-grant"):
            return Reply(json.dumps({label: {"url": f"https://s3.fixture/{label}",
                                              "bucket": "fixture", "key": label,
                                              "headers": {"x-amz-checksum-sha256": "signed"}}
                                     for label in ("archive", "manifest")}).encode())
        if request.get_method() == "PUT":
            return Reply(headers={"x-amz-version-id": "immutable-first"})
        return Reply(json.dumps({"id": self.env["ADP_CHECKPOINT_ID"], "status": "verified"}).encode())

    def run_helper(self, opener=None):
        with patch("crew_checkpoint_helper.Path.read_text", return_value="projected-token"), \
             patch("crew_checkpoint_helper.ssl.create_default_context", return_value=object()):
            return run(self.env, opener=opener or self.open, home=self.home)

    def test_stages_mounted_home_and_completes_only_recorded_versions(self):
        self.assertEqual(self.run_helper(), {"id": self.env["ADP_CHECKPOINT_ID"], "status": "verified"})
        self.assertEqual([request.get_method() for request, _ in self.requests],
                         ["POST", "PUT", "PUT", "POST"])
        grant = json.loads(self.requests[0][0].data)
        completion = json.loads(self.requests[-1][0].data)
        self.assertEqual(grant["owner_key"], self.env["ADP_OWNER_KEY"])
        self.assertEqual(completion["objects"]["archive"]["version_id"], "immutable-first")
        self.assertEqual(completion["objects"]["manifest"]["version_id"], "immutable-first")
        self.assertEqual([request.get_header("X-amz-checksum-sha256") for request, _ in self.requests[1:3]],
                         ["signed", "signed"])
        self.assertNotIn("projected-token", json.dumps(completion))
        self.assertNotIn("/var/run/adp-checkpoint/token", json.dumps(completion))

    def test_unverified_or_timed_out_completion_never_reports_success(self):
        for status in ("uploading", "failed"):
            self.requests.clear()

            def unverified(request, *, timeout, context):
                if request.full_url.endswith("/complete"):
                    self.requests.append((request, context))
                    return Reply(json.dumps({"id": self.env["ADP_CHECKPOINT_ID"], "status": status}).encode())
                return self.open(request, timeout=timeout, context=context)

            with self.assertRaisesRegex(InvalidCheckpoint, "verification pending or failed"):
                self.run_helper(unverified)
            self.assertEqual(len(self.requests), 4)

        def timed_out(request, *, timeout, context):
            if request.full_url.endswith("/complete"):
                raise TimeoutError("verification timed out")
            return self.open(request, timeout=timeout, context=context)

        with self.assertRaisesRegex(TimeoutError, "verification timed out"):
            self.run_helper(timed_out)

    def test_failed_or_unversioned_upload_cannot_request_completion(self):
        def unversioned(request, *, timeout, context):
            if request.get_method() == "PUT":
                self.requests.append((request, context))
                return Reply()
            return self.open(request, timeout=timeout, context=context)

        with self.assertRaisesRegex(InvalidCheckpoint, "unversioned"):
            self.run_helper(unversioned)
        self.assertEqual(len(self.requests), 2)

    def test_restore_refuses_newer_divergent_mount_before_requesting_a_grant(self):
        from hashlib import sha256

        mount = self.home.parent / "retained-mount"
        mount.mkdir()
        newer = mount / "lessons.jsonl"
        newer.write_bytes(b'{"provenance":"newer-unsynced-local"}\n')
        original_digest = sha256(newer.read_bytes()).hexdigest()
        requests = []

        def unexpected_request(*args, **kwargs):
            requests.append(args)
            raise AssertionError("nonempty mount must not request a restore grant")

        with self.assertRaisesRegex(InvalidCheckpoint, "empty mounted home"):
            run_restore(self.env, opener=unexpected_request, home=mount)
        self.assertEqual(requests, [])
        self.assertEqual(sha256(newer.read_bytes()).hexdigest(), original_digest)
        self.assertEqual([item.name for item in mount.iterdir()], ["lessons.jsonl"])

    def test_restore_entrypoint_releases_pin_and_refuses_corrupt_exact_version(self):
        from hashlib import sha256

        root = Path(self.home).parent
        archive_path, manifest_path = root / "archive.tar", root / "manifest.json"
        capture(self.home, archive_path, manifest_path)
        content = {"archive": archive_path.read_bytes(), "manifest": manifest_path.read_bytes()}
        receipt = {label: {"bucket": "fixture", "key": label, "version_id": "first",
                           "sha256": sha256(data).hexdigest(), "size": len(data),
                           "url": "https://s3.fixture/" + label}
                   for label, data in content.items()}
        release_calls = []
        corrupt = [True]

        def restore_open(request, *, timeout, context):
            if request.full_url.endswith("/restore-grant"):
                return Reply(json.dumps({"id": self.env["ADP_CHECKPOINT_ID"], "restore_token": "p" * 32,
                                         "crew_version": "0.11.2", "schema_version": 1,
                                         "objects": receipt}).encode())
            if request.full_url.endswith("/restore-release"):
                release_calls.append(json.loads(request.data))
                return Reply()
            label = request.full_url.split("/")[-1]
            return Reply(b"corrupt" if corrupt[0] and label == "archive" else content[label],
                         {"x-amz-version-id": "first"})

        mount = root / "replacement-volume"
        mount.mkdir()
        with patch("crew_checkpoint_helper.Path.read_text", return_value="projected-token"), \
             patch("crew_checkpoint_helper.ssl.create_default_context", return_value=object()):
            with self.assertRaisesRegex(InvalidCheckpoint, "checksum"):
                run_restore(self.env, opener=restore_open, home=mount)
            self.assertEqual(list(mount.iterdir()), [])
            self.assertEqual(release_calls, [{"restore_token": "p" * 32}])
            corrupt[0] = False
            result = run_restore(self.env, opener=restore_open, home=mount)
        self.assertEqual(result["status"], "restored")
        self.assertTrue((mount / "memory.db").is_file())
        self.assertEqual(len(release_calls), 2)

    def test_rejects_other_mount_or_gateway_before_reading_credentials(self):
        self.env["KIROCREW_HOME"] = "/wrong-owner"
        with self.assertRaisesRegex(InvalidCheckpoint, "mount"):
            run(self.env, opener=self.open, home=self.home)
        self.env["KIROCREW_HOME"] = "/mnt/crew"
        self.env["ADP_CHAT_DATA_URL"] = "https://public.example:8443"
        with self.assertRaisesRegex(InvalidCheckpoint, "gateway"):
            run(self.env, opener=self.open, home=self.home)
        self.assertEqual(self.requests, [])

    def test_helper_cannot_claim_final_handoff_without_writer_pause(self):
        with self.assertRaisesRegex(InvalidCheckpoint, "writer quiescence"):
            with patch("crew_checkpoint_helper.Path.read_text", return_value="projected-token"), \
                 patch("crew_checkpoint_helper.ssl.create_default_context", return_value=object()):
                run(self.env, opener=self.open, home=self.home, quiesced=True)
        self.assertEqual(self.requests, [])

    def test_image_packages_policy_entrypoint(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile.checkpoint-helper").read_text()
        wrapper = (root / "chat-checkpoint-helper").read_text()
        self.assertIn('ENTRYPOINT ["/app/chat-checkpoint-helper"]', dockerfile)
        self.assertIn('USER agent:agent', dockerfile)
        self.assertIn('agent/scripts/crew_checkpoint.py agent/scripts/crew_checkpoint_helper.py', dockerfile)
        self.assertIn('exec python3 /app/crew_checkpoint_helper.py', wrapper)
        self.assertNotIn('AWS_ACCESS_KEY_ID', dockerfile + wrapper)


if __name__ == "__main__":
    unittest.main()
