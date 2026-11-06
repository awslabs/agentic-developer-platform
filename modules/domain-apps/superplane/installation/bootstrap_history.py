"""Separate authenticated historical statements from current owner context.

This never reconstructs a full historical installation receipt. Versioned archive
members establish what was recorded; live owner/lock/object checks establish the
current context used by the existing bootstrap collector.
"""

import copy
import hashlib
import io
import json
from pathlib import PurePosixPath
import re
import tarfile

from .config import LABEL, digest, identity
from .provider_authority import require
from .runtime_maintenance import payload

FIELDS = (
    "account_id",
    "region",
    "environment",
    "cluster",
    "namespace",
    "org_id",
    "adp_org_id",
)
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_MEMBER = 2 * 1024 * 1024


def archived_member(installer, reference, session):
    require(installer.bucket == f"adp-terraform-state-{installer.env['account_id']}")
    require(
        isinstance(reference, dict)
        and set(reference)
        == {
            "key",
            "version_id",
            "archive_sha256",
            "member",
            "member_sha256",
        }
    )
    require(all(isinstance(v, str) and v for v in reference.values()))
    require(
        all(
            re.fullmatch(r"[a-f0-9]{64}", reference[k])
            for k in ("archive_sha256", "member_sha256")
        )
    )
    key, member = reference["key"], reference["member"]
    require(
        key.startswith(f"domain-evidence/{installer.env['environment']}/superplane/")
        and PurePosixPath(key).name == reference["archive_sha256"] + ".tar.gz"
        and not member.startswith("/")
        and ".." not in PurePosixPath(member).parts
        and reference["version_id"] != "null"
    )
    response = session.client("s3", region_name=installer.env["region"]).get_object(
        Bucket=installer.bucket,
        Key=key,
        VersionId=reference["version_id"],
    )
    stream = response["Body"]
    try:
        require(
            response.get("VersionId") == reference["version_id"]
            and type(response.get("ContentLength")) is int
            and 0 < response["ContentLength"] <= MAX_ARCHIVE
        )
        raw = stream.read(MAX_ARCHIVE + 1)
    finally:
        stream.close()
    require(
        len(raw) == response["ContentLength"]
        and hashlib.sha256(raw).hexdigest() == reference["archive_sha256"]
    )
    found, count, expanded = [], 0, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for entry in archive:
            count += 1
            expanded += entry.size
            require(
                count <= 4096
                and 0 <= entry.size <= 128 * 1024 * 1024
                and expanded <= 128 * 1024 * 1024
            )
            if entry.name == member:
                require(entry.isfile() and 0 < entry.size <= MAX_MEMBER)
                with archive.extractfile(entry) as stream:
                    found.append(stream.read(MAX_MEMBER + 1))
    require(
        len(found) == 1
        and hashlib.sha256(found[0]).hexdigest() == reference["member_sha256"]
    )
    document = json.loads(found[0])
    require(isinstance(document, dict))
    return document


def current_context(installer, enrollment, session, *, lock=None):
    """Fresh owner, native lock, management TLS context and object ownership."""
    require(installer is not None)
    if lock is None:
        installer.verify_deployment_identity()
        lock = installer.reconcile_lock_attempt()
    require(lock is not None and lock == installer.receipt.get("remote_lock"))
    env, authority = installer.env, enrollment["authority"]
    require(
        installer.owner == identity(env) == authority["installation_id"]
        and env["account_id"] == authority["account_id"]
        and env["environment"] == enrollment["environment"]
        and env["region"] == enrollment.get("management_region", authority["region"])
        and env["cluster"] == enrollment["management_cluster"]
        and env["org_id"] == authority["org_id"]
        and env["adp_org_id"] == authority["adp_org_id"]
    )
    cluster = session.client("eks", region_name=env["region"]).describe_cluster(
        name=env["cluster"]
    )["cluster"]
    configured = installer.json(
        installer.kube(
            "config",
            "view",
            "--minify",
            "--raw",
            "-o",
            "jsonpath={.clusters}",
        )
    )
    require(
        cluster.get("arn")
        == f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{env['cluster']}"
        and cluster.get("status") == "ACTIVE"
        and len(configured) == 1
        and configured[0]["cluster"].get("server") == cluster["endpoint"]
        and configured[0]["cluster"].get("certificate-authority-data")
        == cluster["certificateAuthority"]["data"]
        and not configured[0]["cluster"].get("insecure-skip-tls-verify")
    )
    observed = {}
    for kind, name in (
        ("Namespace", env["namespace"]),
        ("Deployment", "superplane-api"),
        ("ScaledJob", "superplane-paid-worker"),
    ):
        args = [] if kind == "Namespace" else ["-n", env["namespace"]]
        value = installer.json(installer.kube("get", kind, name, *args, "-o", "json"))
        meta = value["metadata"]
        require(
            value["kind"] == kind
            and meta["name"] == name
            and meta.get("uid")
            and not meta.get("deletionTimestamp")
            and meta.get("labels", {}).get(LABEL) == installer.owner
            and (kind == "Namespace" or meta.get("namespace") == env["namespace"])
        )
        observed[kind] = {
            "uid": meta["uid"],
            "configuration_sha256": digest(payload(value)),
        }
    require(installer.reconcile_lock_attempt() == lock)
    return {
        "environment_sha256": digest(env),
        "identity": {k: env[k] for k in FIELDS},
        "objects": observed,
        "installation_lock": copy.deepcopy(lock),
    }


def qualify(
    installer, enrollment, original, references, session, *, archive_read_session=None
):
    require(
        installer is not None
        and isinstance(references, dict)
        and set(references) == {"verified_statement", "recovery_preparation"}
    )
    installer.verify_deployment_identity()
    lock = installer.reconcile_lock_attempt()
    require(lock is not None and lock == installer.receipt.get("remote_lock"))
    archive_reader = archive_read_session or session
    reader_identity = archive_reader.client(
        "sts", region_name=installer.env["region"]
    ).get_caller_identity()
    require(
        reader_identity.get("Account") == installer.env["account_id"]
        and all(
            isinstance(reader_identity.get(k), str) and reader_identity[k]
            for k in ("Arn", "UserId")
        )
    )
    statement = archived_member(
        installer, references["verified_statement"], archive_reader
    )
    preparation = archived_member(
        installer, references["recovery_preparation"], archive_reader
    )
    authority, env = enrollment["authority"], installer.env
    require(
        statement == original
        and "environment" not in statement
        and statement.get("version") == 1
        and statement.get("status") == "installed-and-verified"
        and re.fullmatch(
            r"[a-f0-9]{64}", str(statement.get("original_receipt_sha256", ""))
        )
    )
    require(
        preparation.get("version") == 1
        and preparation.get("status") == "recovery-credentials-prepared"
        and preparation.get("installation_id")
        == statement.get("installation_id")
        == installer.owner
        == identity(env)
        == authority["installation_id"]
    )
    require(
        all(preparation.get("environment", {}).get(k) == env[k] for k in FIELDS)
        and env["account_id"] == authority["account_id"]
        and env["environment"] == enrollment["environment"]
        and env["region"] == enrollment.get("management_region", authority["region"])
        and env["cluster"] == enrollment["management_cluster"]
        and env["org_id"] == authority["org_id"]
        and env["adp_org_id"] == authority["adp_org_id"]
    )
    current = current_context(installer, enrollment, session, lock=lock)
    require(current["installation_lock"] == lock)
    return {
        "kind": "archived-installation-current-context-v1",
        "historical": {
            "archives": copy.deepcopy(references),
            "archive_reader": {
                k: reader_identity[k] for k in ("Account", "Arn", "UserId")
            },
            "installation_id": statement["installation_id"],
            "verified_statement_run_id": statement["run_id"],
            "preparation_run_id": preparation["run_id"],
            "original_full_receipt_sha256_reference": statement[
                "original_receipt_sha256"
            ],
            "full_original_receipt_recovered": False,
        },
        "current": current,
    }
