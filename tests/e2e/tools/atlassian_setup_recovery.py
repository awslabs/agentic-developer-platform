"""Publish and independently read back redacted, owner-bound setup recovery state."""

import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from tests.e2e.tools.atlassian_setup_live import (
    GatewaySetupDriver,
    load_setup,
    private_file,
)
from tests.e2e.tools.atlassian_integration import SetupActionLedger
from tests.e2e.tools.atlassian_setup_observer import IndependentSetupObserver
from tests.e2e.tools.evidence_reader import (
    DIGEST,
    VERSION,
    archived_files,
    checked_object,
    fetch_s3,
    require,
)


SNAPSHOT = "setup-recovery.json"


def projection(config):
    ledger, _ = load_setup(
        os.environ["ADP_TOOLS_ATLASSIAN_SETUP_FILE"], gateway_url=config["gateway_url"]
    )
    attempt = GatewaySetupDriver(config)._attempt()
    phase = attempt["phase"] if attempt else "absent"
    return {
        "version": 1,
        "run_id": config["run_id"],
        "owner_id": config["owner_id"],
        "fixture_kind": config["fixture_kind"],
        "gateway_sha256": hashlib.sha256(config["gateway_url"].encode()).hexdigest(),
        "phase": phase,
        "connection_id": attempt["connection_id"] if attempt else None,
        "revision": attempt["revision"] if attempt else None,
        "actions": ledger["ledger"].state()["actions"],
    }


def capture(config, destination):
    data = json.dumps(projection(config), sort_keys=True, separators=(",", ":"))
    root = Path(destination)
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        raise ValueError("Private setup recovery directory unavailable")
    root.chmod(0o700)
    path = root / SNAPSHOT
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return path


def verify_receipt(uri, version, *, config, run_id, attempt, revision, fetch=fetch_s3):
    account = config["account"]
    require(
        bool(re.fullmatch(r"[0-9]{12}", account))
        and bool(re.fullmatch(r"[1-9][0-9]*", run_id))
        and bool(re.fullmatch(r"[1-9][0-9]*", attempt))
        and bool(re.fullmatch(r"[0-9a-f]{40}", revision))
    )
    bucket = f"adp-dev-ci-evidence-{account}"
    prefix = (
        f"artifacts/14/aws-e/adp/{run_id}/{attempt}/evaluate/"
        f"tools-live-evaluation-{run_id}-{attempt}/"
    )
    require(
        isinstance(uri, str)
        and bool(
            re.fullmatch(
                re.escape(f"s3://{bucket}/{prefix}") + r"[0-9a-f]{64}\.manifest\.json",
                uri,
            )
        )
        and isinstance(version, str)
        and bool(VERSION.fullmatch(version))
    )
    key = uri.removeprefix(f"s3://{bucket}/")
    payload, metadata = fetch(bucket, key)
    digest = hashlib.sha256(payload).digest()
    require(
        type(metadata) is dict
        and metadata.get("VersionId") == version
        and metadata.get("ChecksumSHA256") == base64.b64encode(digest).decode()
        and metadata.get("ContentLength") == len(payload)
        and digest.hex() == key.rsplit("/", 1)[-1].split(".", 1)[0]
    )
    manifest = json.loads(payload)
    require(
        type(manifest) is dict
        and manifest.get("schema_version") == 1
        and manifest.get("repository") == "aws-e/adp"
        and manifest.get("run_id") == run_id
        and manifest.get("run_attempt") == attempt
        and manifest.get("job") == "evaluate"
        and manifest.get("name") == f"tools-live-evaluation-{run_id}-{attempt}"
        and manifest.get("retention_days") == 14
        and manifest.get("checked_out_revision") == revision
        and manifest.get("workflow_revision") == revision
        and not manifest.get("missing_path_count")
    )
    artifact = manifest.get("artifact", {})
    require(
        type(artifact) is dict
        and artifact.get("bucket") == bucket
        and artifact.get("key") == f"{prefix}{artifact.get('sha256')}.tar.gz"
        and isinstance(artifact.get("sha256"), str)
        and bool(DIGEST.fullmatch(artifact["sha256"]))
    )
    archive = checked_object(
        bucket,
        artifact["key"],
        artifact.get("version_id"),
        artifact["sha256"],
        artifact.get("size"),
        fetch,
    )
    files = archived_files(archive, manifest.get("files"), required=(SNAPSHOT,))
    snapshot = json.loads(files[SNAPSHOT])
    require(
        type(snapshot) is dict
        and set(snapshot)
        == {
            "version",
            "run_id",
            "owner_id",
            "fixture_kind",
            "gateway_sha256",
            "phase",
            "connection_id",
            "revision",
            "actions",
        }
        and snapshot.get("version") == 1
        and all(
            snapshot.get(key) == config[key]
            for key in ("run_id", "owner_id", "fixture_kind")
        )
        and snapshot.get("gateway_sha256")
        == hashlib.sha256(config["gateway_url"].encode()).hexdigest()
        and snapshot.get("phase")
        in {"absent", "starting", "pending", "completed", "selected", "cleaned"}
        and type(snapshot.get("actions")) is list
    )
    return files[SNAPSHOT], snapshot


def restore_selected(config, snapshot, *, driver, observer, ledger):
    require(
        snapshot.get("phase") == "selected"
        and type(snapshot.get("connection_id")) is str
        and bool(snapshot["connection_id"])
        and type(snapshot.get("revision")) is str
        and bool(snapshot["revision"])
        and type(snapshot.get("actions")) is list
        and bool(snapshot["actions"])
        and driver._attempt() is None
        and not ledger.state()["actions"]
    )
    status = observer._gateway_status()
    binding = status.get("connection") or {}
    require(
        type(binding) is dict
        and binding.get("id") == snapshot["connection_id"]
        and binding.get("revision") == snapshot["revision"]
        and binding.get("selected_site_id") == config["site_id"]
    )
    for action in snapshot["actions"]:
        require(
            type(action) is dict
            and set(action) == {"action", "actor", "status", "evidence_sha256"}
            and action["action"]
            in SetupActionLedger.MACHINE_ACTIONS
            | SetupActionLedger.HUMAN_ACTIONS
            | SetupActionLedger.POLICY_ACTIONS
            and action["actor"]
            == (
                "machine"
                if action["action"]
                in SetupActionLedger.MACHINE_ACTIONS | SetupActionLedger.POLICY_ACTIONS
                else "human"
            )
            and action["status"] in {"started", "observed", "blocked", "unknown"}
            and (
                action["evidence_sha256"] is None
                or type(action["evidence_sha256"]) is str
                and bool(DIGEST.fullmatch(action["evidence_sha256"]))
            )
            and (
                action["status"] != "observed" or action["evidence_sha256"] is not None
            )
        )
    driver._save_attempt(
        {
            "run_id": config["run_id"],
            "owner_id": config["owner_id"],
            "fixture_kind": config["fixture_kind"],
            "phase": "selected",
            "state": None,
            "session_uri": None,
            "authorization_url": None,
            "connection_id": snapshot["connection_id"],
            "revision": snapshot["revision"],
        }
    )
    for action in snapshot["actions"]:
        ledger.record(**action)


def main():
    try:
        fixture, config = load_setup(
            os.environ["ADP_TOOLS_ATLASSIAN_SETUP_FILE"],
            gateway_url=os.environ["GATEWAY_URL"],
        )
        if len(sys.argv) != 2 or sys.argv[1] not in {"capture", "verify", "restore"}:
            raise ValueError("Expected setup recovery capture or verification")
        path = Path(os.environ["RUNNER_TEMP"]) / "tools-eval-evidence" / SNAPSHOT
        if sys.argv[1] == "capture":
            capture(config, path.parent)
        elif sys.argv[1] == "verify":
            retained, _ = verify_receipt(
                os.environ["CI_EVIDENCE_MANIFEST_URI"],
                os.environ["CI_EVIDENCE_MANIFEST_VERSION"],
                config=config,
                run_id=os.environ["GITHUB_RUN_ID"],
                attempt=os.environ["GITHUB_RUN_ATTEMPT"],
                revision=os.environ["GITHUB_SHA"],
            )
            require(private_file(path, max_bytes=65536) == retained)
            with open(os.environ["GITHUB_ENV"], "a") as output:
                output.write("ADP_TOOLS_ATLASSIAN_RECOVERY_VERIFIED=1\n")
        else:
            uri = os.environ["ADP_TOOLS_ATLASSIAN_RESUME_URI"]
            match = re.fullmatch(
                rf"s3://adp-dev-ci-evidence-{config['account']}/artifacts/14/aws-e/adp/"
                r"([1-9][0-9]*)/([1-9][0-9]*)/evaluate/tools-live-evaluation-"
                r"[1-9][0-9]*-[1-9][0-9]*/[0-9a-f]{64}\.manifest\.json",
                uri,
            )
            require(match is not None)
            _, snapshot = verify_receipt(
                uri,
                os.environ["ADP_TOOLS_ATLASSIAN_RESUME_VERSION"],
                config=config,
                run_id=match.group(1),
                attempt=match.group(2),
                revision=os.environ["ADP_TOOLS_ATLASSIAN_RESUME_REVISION"],
            )
            restore_selected(
                config,
                snapshot,
                driver=GatewaySetupDriver(config),
                observer=IndependentSetupObserver(config),
                ledger=fixture["ledger"],
            )
    except Exception:
        print("Protected setup recovery evidence unavailable", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
