"""Versioned, non-expiring app evidence for one paid-image dispatch per source."""

import base64
import hashlib
import json
import tempfile
from pathlib import Path


class BuildRefused(Exception):
    pass


def require(condition, message):
    if not condition:
        raise BuildRefused(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class DispatchClaim:
    def __init__(self, aws, bucket, account, project, config):
        self.aws, self.bucket, self.account = aws, bucket, account
        self.dispatch_id = hashlib.sha256(
            canonical(
                {
                    "account": account,
                    "region": config["region"],
                    "project": project,
                    "source_sha": config["source_sha"],
                }
            )
        ).hexdigest()
        self.prefix = (
            "superplane/releases/paid-worker/dispatch/" + self.dispatch_id + "/"
        )
        self.config_sha256 = hashlib.sha256(canonical(config)).hexdigest()
        self.child_etag = None

    def retained(self):
        versioning = self.aws(
            "s3api",
            "get-bucket-versioning",
            "--bucket",
            self.bucket,
            "--expected-bucket-owner",
            self.account,
        )
        require(
            versioning.get("Status") == "Enabled",
            "dispatch evidence requires an owned versioned bucket",
        )
        lifecycle = self.aws(
            "s3api",
            "get-bucket-lifecycle-configuration",
            "--bucket",
            self.bucket,
            "--expected-bucket-owner",
            self.account,
        )
        require(
            isinstance(lifecycle.get("Rules"), list),
            "dispatch evidence retention is unavailable",
        )
        for rule in lifecycle["Rules"]:
            if rule.get("Status") != "Enabled":
                continue
            filtering = rule.get("Filter", {})
            require(
                isinstance(filtering, dict), "dispatch lifecycle filter is unavailable"
            )
            prefix = filtering.get(
                "Prefix", filtering.get("And", {}).get("Prefix", rule.get("Prefix", ""))
            )
            require(isinstance(prefix, str), "dispatch lifecycle prefix is unavailable")
            if any(
                (self.prefix + name).startswith(prefix)
                for name in ("claim.json", "child.json")
            ):
                require(
                    not set(rule)
                    & {
                        "Expiration",
                        "NoncurrentVersionExpiration",
                        "Transitions",
                        "NoncurrentVersionTransitions",
                    },
                    "dispatch claim/evidence must never expire or be archived by bucket lifecycle",
                )

    def put(self, suffix, value, *, etag=None):
        raw = canonical(value)
        checksum = base64.b64encode(hashlib.sha256(raw).digest()).decode()
        with tempfile.TemporaryDirectory(prefix="superplane-paid-claim-") as directory:
            path = Path(directory) / "evidence.json"
            path.write_bytes(raw)
            result = self.aws(
                "s3api",
                "put-object",
                "--bucket",
                self.bucket,
                "--key",
                self.prefix + suffix,
                "--expected-bucket-owner",
                self.account,
                "--body",
                str(path),
                "--checksum-sha256",
                checksum,
                *(["--if-match", etag] if etag else ["--if-none-match", "*"]),
            )
        require(
            result.get("VersionId") not in (None, "", "null")
            and result.get("ChecksumSHA256") == checksum
            and result.get("ETag"),
            "dispatch evidence write unconfirmed; retain unknown outcome and never repeat start",
        )
        return result

    def claim(self, state):
        self.retained()
        return self.put(
            "claim.json",
            {
                "version": 1,
                "dispatch_id": self.dispatch_id,
                "config_sha256": self.config_sha256,
                "project": state["project"],
                "source_sha": state["config"]["source_sha"],
                "state": "CLAIMED_START_NOT_CONFIRMED",
            },
        )

    def record(self, state):
        result = self.put("child.json", state, etag=self.child_etag)
        self.child_etag = result["ETag"]
