"""Retain encrypted original recovery state through the existing private CI store."""

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from tests.e2e.tools.atlassian_client_matrix import (
    CLIENTS,
    ClientOperationLedger,
    digest,
    load_isolated_fixtures,
    recovery_roots,
)
from tests.e2e.tools.atlassian_fixture_cleanup import JiraFixtureLedger
from tests.e2e.tools.atlassian_recovery_consumer import binding, read_json, save


ROOT = Path(__file__).resolve().parents[3]
SCHEMA = "atlassian-recovery-handoff/v1"
ORIGIN = "recovery-origin.json"
PLAN = "recovery-plan.json"


def protected_key(value):
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ValueError("Protected recovery key is unavailable") from None
    if len(key) != 32:
        raise ValueError("Protected recovery key is unavailable")
    return key


def publication_name(client, run_id, attempt):
    if client not in CLIENTS or any(
        not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value)
        for value in (run_id, attempt)
    ):
        raise ValueError("Original workflow recovery binding required")
    return f"tools-recovery-{client}-{run_id}-{attempt}"


def read_publication(receipt, *, account, client, fetch=None):
    from tests.e2e.tools.evidence_reader import (
        VERSION,
        archived_files,
        checked_object,
        decode_report,
        fetch_s3,
        require,
    )

    fetch = fetch or fetch_s3
    require(
        type(receipt) is dict
        and set(receipt) == {"uri", "version", "run_id", "attempt", "revision"}
    )
    name = publication_name(client, receipt["run_id"], receipt["attempt"])
    require(
        isinstance(account, str)
        and re.fullmatch(r"[0-9]{12}", account)
        and isinstance(receipt["revision"], str)
        and re.fullmatch(r"[0-9a-f]{40}", receipt["revision"])
        and isinstance(receipt["version"], str)
        and receipt["version"] != "null"
        and VERSION.fullmatch(receipt["version"])
    )
    bucket = f"adp-dev-ci-evidence-{account}"
    prefix = f"artifacts/14/aws-e/adp/{receipt['run_id']}/{receipt['attempt']}/evaluate/{name}/"
    require(
        isinstance(receipt["uri"], str)
        and re.fullmatch(
            re.escape(f"s3://{bucket}/{prefix}") + r"[0-9a-f]{64}\.manifest\.json",
            receipt["uri"],
        )
    )
    manifest_key = receipt["uri"].removeprefix(f"s3://{bucket}/")
    payload, metadata = fetch(bucket, manifest_key)
    checksum = hashlib.sha256(payload).digest()
    require(
        len(payload) <= 65536
        and type(metadata) is dict
        and metadata.get("VersionId") == receipt["version"]
        and metadata.get("ContentLength") == len(payload)
        and metadata.get("ChecksumSHA256") == base64.b64encode(checksum).decode()
        and checksum.hex() == manifest_key.rsplit("/", 1)[-1].split(".")[0]
    )
    manifest = decode_report(payload)
    require(type(manifest) is dict and type(manifest.get("schema_version")) is int)
    require(
        all(
            manifest.get(key) == value
            for key, value in {
                "schema_version": 1,
                "repository": "aws-e/adp",
                "job": "evaluate",
                "run_id": receipt["run_id"],
                "run_attempt": receipt["attempt"],
                "name": name,
                "retention_days": 14,
                "checked_out_revision": receipt["revision"],
                "workflow_revision": receipt["revision"],
            }.items()
        )
        and not manifest.get("missing_path_count")
    )
    artifact = manifest.get("artifact", {})
    require(
        type(artifact) is dict
        and artifact.get("bucket") == bucket
        and isinstance(artifact.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        and artifact.get("key") == f"{prefix}{artifact['sha256']}.tar.gz"
    )
    archive = checked_object(
        bucket,
        artifact["key"],
        artifact.get("version_id"),
        artifact["sha256"],
        artifact.get("size"),
        fetch,
    )
    files = archived_files(
        archive, manifest.get("files"), required=("capsule.json", "binding.json")
    )
    require(set(files) == {"capsule.json", "binding.json"})
    record = decode_report(files["binding.json"])
    require(
        type(record) is dict
        and set(record) == {"schema", "binding", "archive_sha256"}
        and record["schema"] == SCHEMA
        and type(record["binding"]) is dict
        and record["binding"].get("client") == client
        and record["archive_sha256"]
        == hashlib.sha256(files["capsule.json"]).hexdigest()
    )
    return files["capsule.json"], record


def publish(directory, client):
    env = dict(os.environ)
    if (
        env.get("GITHUB_REPOSITORY") != "aws-e/adp"
        or env.get("GITHUB_JOB") != "evaluate"
    ):
        raise ValueError("Protected evaluation publication context required")
    name = publication_name(client, env["GITHUB_RUN_ID"], env["GITHUB_RUN_ATTEMPT"])
    with tempfile.TemporaryDirectory(
        prefix="recovery-upload-", dir=env["RUNNER_TEMP"]
    ) as scratch:
        output = Path(scratch) / "outputs"
        env.update(
            CI_EVIDENCE_NAME=name,
            CI_EVIDENCE_PATH=str(directory),
            CI_EVIDENCE_RETENTION_DAYS="14",
            CI_EVIDENCE_IF_NO_FILES_FOUND="error",
            GITHUB_OUTPUT=str(output),
        )
        result = subprocess.run(
            [sys.executable, str(ROOT / ".github/scripts/upload_ci_evidence_s3.py")],
            env=env,
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("Encrypted recovery publication failed")
        fields = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return {
        "uri": fields["manifest-uri"],
        "version": fields["manifest-version"],
        "run_id": env["GITHUB_RUN_ID"],
        "attempt": env["GITHUB_RUN_ATTEMPT"],
        "revision": env["GITHUB_SHA"],
    }


def rebind(matrix, client, destination, expected):
    matrix.close()
    fixture = matrix.fixtures[client]
    ledger = ClientOperationLedger(
        destination / "operations.json", run_id=fixture["ledger"].run_id
    )
    fixture_ledger = JiraFixtureLedger(
        destination / "jira-fixture.json", **matrix.fixture_ledgers[client].identity
    )
    if fixture_ledger.state() is None:
        raise ValueError("Restored fixture ownership is unavailable")
    plan = read_json(destination / PLAN)
    if digest(plan) != expected["plan_sha256"]:
        raise ValueError("Restored original plan differs")
    previous, previous_plan, previous_ledger = (
        fixture,
        matrix.plans[client],
        matrix.fixture_ledgers[client],
    )
    matrix.fixtures[client] = {**fixture, "ledger": ledger}
    matrix.plans[client] = plan
    matrix.fixture_ledgers[client] = fixture_ledger
    try:
        matrix._resume(client)
        if binding(matrix, client) != expected:
            raise ValueError("Restored original authority differs")
    except Exception:
        matrix.close()
        (
            matrix.fixtures[client],
            matrix.plans[client],
            matrix.fixture_ledgers[client],
        ) = previous, previous_plan, previous_ledger
        raise
    matrix.close()


def persist_and_restore(
    matrix,
    client,
    *,
    key,
    parent,
    account,
    publish_snapshot=publish,
    read_snapshot=read_publication,
    retain_pointer=False,
):
    from tests.e2e.tools.atlassian_recovery_archive import restore, seal

    matrix.close()
    source = matrix.fixtures[client]["ledger"].path.parent
    expected = binding(matrix, client)
    origin = source / ORIGIN
    if origin.exists():
        return read_json(origin)
    plan = source / PLAN
    if plan.exists():
        if read_json(plan) != matrix.plans[client]:
            raise ValueError("Retained recovery plan changed")
    else:
        save(plan, matrix.plans[client])
    capsule = seal(source, binding=expected, key=key)
    record = {
        "schema": SCHEMA,
        "binding": expected,
        "archive_sha256": hashlib.sha256(capsule).hexdigest(),
    }
    with tempfile.TemporaryDirectory(
        prefix="recovery-publication-", dir=parent
    ) as scratch:
        directory = Path(scratch)
        save(directory / "binding.json", record)
        descriptor = os.open(
            directory / "capsule.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(capsule)
            stream.flush()
            os.fsync(stream.fileno())
        receipt = publish_snapshot(directory, client)
        retrieved, verified = read_snapshot(receipt, account=account, client=client)
    if retrieved != capsule or verified != record:
        raise ValueError("Published recovery differs from original state")
    save(source / "recovery-publication.json", receipt)
    destination = restore(retrieved, parent=parent, binding=expected, key=key)
    rebind(matrix, client, destination, expected)
    original_identity = source.stat()
    restored_identity = destination.stat()
    if (original_identity.st_dev, original_identity.st_ino) == (
        restored_identity.st_dev,
        restored_identity.st_ino,
    ):
        raise ValueError("Recovery must reopen an independent private root")
    result = {
        "schema": SCHEMA,
        "publication": receipt,
        "binding_sha256": digest(expected),
        "archive_sha256": record["archive_sha256"],
        "restored_without_source": False,
    }
    save(destination / ORIGIN, result)
    if retain_pointer:
        pointer = Path(parent) / "recovery-roots.json"
        selected = read_json(pointer, 8192) if pointer.exists() else {}
        selected[client] = destination.name
        save(pointer, selected)
    shutil.rmtree(source)
    if source.exists():
        raise ValueError("Original recovery filesystem remains available")
    result["restored_without_source"] = True
    save(destination / ORIGIN, result)
    return result


def workflow_handoff(matrix, client):
    return persist_and_restore(
        matrix,
        client,
        key=protected_key(os.environ.get("ADP_TOOLS_ATLASSIAN_RECOVERY_KEY_B64")),
        parent=Path(os.environ["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"]).parent,
        account=os.environ["EXPECTED_ACCOUNT"],
        retain_pointer=True,
    )


def restore_retained(
    manifest_file,
    receipts,
    *,
    gateway_url,
    gateway_revision,
    definition_revision,
    account,
    key,
    read_snapshot=None,
):
    from tests.e2e.tools.atlassian_recovery_archive import restore
    from tests.e2e.tools.atlassian_native_matrix import validate_plan

    read_snapshot = read_snapshot or read_publication
    if type(receipts) is not dict or not receipts or not set(receipts) <= CLIENTS:
        raise ValueError("Selected original workflow recovery receipts required")
    fixtures = load_isolated_fixtures(manifest_file)
    parent = Path(manifest_file).parent
    selected = recovery_roots(parent)
    for client, receipt in receipts.items():
        if client in selected:
            if read_json(selected[client] / ORIGIN)["publication"] != receipt:
                raise ValueError("A different recovery already owns this client")
            continue
        capsule, record = read_snapshot(receipt, account=account, client=client)
        expected = record["binding"]
        if (
            receipt["revision"] != definition_revision
            or expected["client"] != client
            or expected["owner_id"] != fixtures[client]["owner_id"]
            or expected["run_id"] != fixtures[client]["ledger"].run_id
            or expected["gateway_revision"] != gateway_revision
            or expected["gateway_sha256"] != digest(gateway_url)
        ):
            raise ValueError(
                "Original recovery differs from the protected owner, run or target"
            )
        destination = restore(capsule, parent=parent, binding=expected, key=key)
        try:
            plan = read_json(destination / PLAN)
            validate_plan(plan, fixtures[client], client)
            if digest(plan) != expected["plan_sha256"]:
                raise ValueError("Original recovery plan is unavailable")
            origin = {
                "schema": SCHEMA,
                "publication": receipt,
                "binding_sha256": digest(expected),
                "archive_sha256": record["archive_sha256"],
                "restored_without_source": not Path(
                    plan["approval_file"]
                ).parent.exists(),
            }
            save(destination / ORIGIN, origin)
            selected[client] = destination
            save(
                parent / "recovery-roots.json",
                {name: path.name for name, path in selected.items()},
            )
        except Exception:
            shutil.rmtree(destination)
            raise
    return {client: selected[client] for client in receipts}


def main():
    try:
        raw = os.environ["ADP_TOOLS_ATLASSIAN_RECOVERY_RESUME_JSON"]
        if len(raw.encode()) > 16384:
            raise ValueError("Recovery selection exceeds its bound")
        from tests.e2e.tools.atlassian_flow_summary import _decode

        restored = restore_retained(
            os.environ["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"],
            _decode(raw),
            gateway_url=os.environ["GATEWAY_URL"],
            gateway_revision=os.environ["GATEWAY_REVISION"],
            definition_revision=os.environ["GITHUB_SHA"],
            account=os.environ["EXPECTED_ACCOUNT"],
            key=protected_key(os.environ.get("ADP_TOOLS_ATLASSIAN_RECOVERY_KEY_B64")),
        )
        from tests.e2e.tools.atlassian_recovery_consumer import main as consume_original

        for client in sorted(restored):
            arguments = [
                "--manifest",
                os.environ["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"],
                "--gateway",
                os.environ["GATEWAY_URL"],
                "--revision",
                os.environ["GATEWAY_REVISION"],
                "--client",
                client,
            ]
            for _ in range(2):
                if consume_original(arguments) != 0:
                    return 1
        roots = recovery_roots(
            Path(os.environ["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"]).parent
        )
        if set(roots) == CLIENTS:
            path = (
                Path(os.environ["ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE"]).parent
                / "native-plans.json"
            )
            save(
                path, {client: read_json(root / PLAN) for client, root in roots.items()}
            )
            if os.environ.get("GITHUB_ENV"):
                with open(os.environ["GITHUB_ENV"], "a") as output:
                    output.write(f"ADP_TOOLS_ATLASSIAN_NATIVE_PLAN_FILE={path}\n")
    except (ValueError, OSError, KeyError, TypeError):
        print(
            "Original protected recovery state could not be restored", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
