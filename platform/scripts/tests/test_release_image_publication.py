"""Portable release builders and promoters obey immutable ECR contracts."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "platform/scripts/release"))
import artifacts  # noqa: E402
import build  # noqa: E402


class ReleasePublicationTests(unittest.TestCase):
    def test_all_four_builds_select_archived_source_and_export_by_digest(self):
        body = b'{"schemaVersion":2}'
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        calls = []

        def run(parts, **kwargs):
            calls.append((parts, kwargs))
            if parts[:2] == ["aws", "ecr"]:
                return "test-password"
            if parts[:2] == ["skopeo", "copy"]:
                destination = Path(str(parts[-1]).removeprefix("dir:"))
                destination.mkdir()
                (destination / "manifest.json").write_bytes(body)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(build, "run", side_effect=run),
            patch.object(
                build, "aws", return_value={"imageDetails": [{"imageDigest": digest}]}
            ) as aws,
        ):
            result = build.images(Path(tmp), "a" * 40, "r-independent-bundle-id")
            self.assertEqual(set(result), set(build.IMAGES))
            self.assertTrue(all(value["digest"] == digest for value in result.values()))
            builds = [
                (args, kwargs)
                for args, kwargs in calls
                if args[:2] == ["bash", "platform/scripts/codebuild-run.sh"]
            ]
            self.assertEqual(len(builds), 4)
            for args, kwargs in builds:
                self.assertIn(
                    "name=IMAGE_TAG,value=" + "a" * 40 + ",type=PLAINTEXT", args
                )
                self.assertEqual(kwargs["env"]["SOURCE_SHA"], "a" * 40)
                self.assertEqual(kwargs["env"]["ADP_RELEASE_BUILD"], "true")
            for call in aws.call_args_list:
                self.assertIn("imageTag=" + "a" * 40, call.args)
            exports = [args for args, _ in calls if args[:2] == ["skopeo", "copy"]]
            self.assertEqual(len(exports), 4)
            self.assertTrue(all(args[-2].endswith("@" + digest) for args in exports))

    def setUp(self):
        self.ecr = Mock()
        self.digest = "sha256:" + "d" * 64
        self.found = {"imageDetails": [{"imageDigest": self.digest}]}
        self.missing = ClientError(
            {"Error": {"Code": "ImageNotFoundException"}}, "DescribeImages"
        )

    def publish(self):
        artifacts.publish_image(
            self.ecr,
            "adp-gateway",
            "release-r123-gateway",
            self.digest,
            Path("/offline/image"),
            "111122223333.dkr.ecr.us-east-1.amazonaws.com",
            Path("/offline/auth"),
        )

    def test_matching_release_tag_is_reused_without_copy(self):
        self.ecr.describe_images.return_value = self.found
        with patch.object(artifacts, "run") as run:
            self.publish()
            run.assert_not_called()

    def test_missing_tag_is_copied_and_verified(self):
        self.ecr.describe_images.side_effect = [self.missing, self.found]
        with patch.object(artifacts, "run") as run:
            self.publish()
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:2], ["skopeo", "copy"])
            self.assertEqual(self.ecr.describe_images.call_count, 2)

    def test_existing_different_digest_is_never_overwritten(self):
        self.ecr.describe_images.return_value = {
            "imageDetails": [{"imageDigest": "sha256:" + "e" * 64}]
        }
        with (
            patch.object(artifacts, "run") as run,
            self.assertRaisesRegex(ValueError, "different image"),
        ):
            self.publish()
        run.assert_not_called()

    def test_registry_denial_is_not_treated_as_missing(self):
        self.ecr.describe_images.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException"}}, "DescribeImages"
        )
        with patch.object(artifacts, "run") as run, self.assertRaises(ClientError):
            self.publish()
        run.assert_not_called()

    def test_wrong_digest_after_copy_is_rejected(self):
        self.ecr.describe_images.side_effect = [
            self.missing,
            {"imageDetails": [{"imageDigest": "wrong"}]},
        ]
        with (
            patch.object(artifacts, "run"),
            self.assertRaisesRegex(ValueError, "digest changed"),
        ):
            self.publish()


if __name__ == "__main__":
    unittest.main()
