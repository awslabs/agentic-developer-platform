#!/usr/bin/env python3
"""E04/E05 on the instance: `adp aws connect`, provisioned and handoff.

The journey logic already exists and was reviewed: `personal_aws_worker.py` in
the package above drives the real `adp aws connect` surface end to end. This
module is the dispatcher adapter for it, so the bundle ships one implementation
rather than a second copy that can drift from the reviewed one.

`personal_aws_worker.py` is shipped into this directory by the bundle and
imported here. Its `execute(config, evidence)` signature is already the shape the
dispatcher expects, so the adapter is thin by design: it supplies the config keys
the worker names differently and lets the worker do the asserting.

The one thing the adapter must do is populate `source_dir`. The reviewed worker
installs the CLI from a staged release directory, and nothing ever created that
directory — so `_stage_release()` downloads the served release into it and
verifies every file against the hashes the orchestrator derived from the revision
under test. Fetching from the gateway keeps the subject "the published release",
and the hash check is what stops a partially-served or stale download from being
what E04 and E05 exercise.
"""

from __future__ import annotations

import hashlib
import urllib.error
import urllib.request
from pathlib import Path

import common
from common import require

# The worker's own contract, restated so a missing key fails here — naming the
# key — rather than as a KeyError several hundred lines into a live journey.
# `source_dir` is absent: the adapter stages it below.
REQUIRED = (
    "mode",
    "instance_id",
    "platform_account",
    "destination_account",
    "region",
    "gateway_url",
    "sts_endpoint",
    "secrets_endpoint",
    "connection_name",
    "stack_name",
    "role_name",
    "credential_secret",
    "session_key",
    "provisioner_arn",
    "absent_connection_id",
    "expected_hashes",
)

STAGE_DIR = "/home/ec2-user/adp-eval/release"


def _stage_release(config, evidence):
    """Download the served release into a private directory, hash-verified."""
    expected = config.get("expected_hashes") or {}
    require(
        expected,
        "No expected release hashes were supplied; a stale or partial release "
        "could not be detected and the journey would test unknown code",
    )
    target = Path(config.get("source_dir") or STAGE_DIR)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    gateway = config["gateway_url"].rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    staged, mismatched = {}, []
    for name, digest in sorted(expected.items()):
        url = f"{gateway}/cli/{name}"
        try:
            with opener.open(url, timeout=120) as response:
                require(
                    response.status == 200, f"/cli/{name} returned {response.status}"
                )
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise common.RemoteError(f"/cli/{name} returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise common.RemoteError(
                f"/cli/{name} is unreachable: {type(exc).__name__}"
            ) from None
        actual = hashlib.sha256(payload).hexdigest()
        staged[name] = actual
        if actual != digest:
            mismatched.append(name)
        path = target / name
        path.write_bytes(payload)
        path.chmod(0o700)
    evidence["staged_release"] = {"files": sorted(staged), "verified": not mismatched}
    require(
        not mismatched,
        "The served release does not match the revision under test: "
        + ", ".join(mismatched),
    )
    return str(target)


def execute(config, evidence):
    missing = [key for key in REQUIRED if not config.get(key)]
    require(
        not missing,
        "The personal-AWS journey was invoked without: " + ", ".join(missing),
    )
    require(
        config["mode"] in ("provision", "handoff"),
        f"Unknown personal-AWS mode {config['mode']!r}",
    )
    evidence["stage"] = "stage_release"
    config = {**config, "source_dir": _stage_release(config, evidence)}
    try:
        import personal_aws_worker
    except ImportError as exc:  # pragma: no cover - the bundle asserts this ships
        raise common.RemoteError(
            "personal_aws_worker.py was not shipped with the bundle; "
            "the reviewed journey implementation is missing"
        ) from exc
    personal_aws_worker.execute(config, evidence)


if __name__ == "__main__":
    import sys

    sys.exit(common.run_script(execute))
