"""Preflight and gate a release using one originally approved native smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .native_smoke import SearchFixture, retain, verify
from .native_smoke_inputs import job, private_write
from .native_test_session import (
    AdmissionUnavailable,
    ApprovedReference,
    JWT,
    NativeSession,
    private_file,
    unique_object,
    canonical,
)
from .target_metadata import target_revision
from . import operational_release


def preflight(environ=None, *, now=None):
    """Validate all private inputs without network I/O or creating authority."""
    env = dict(os.environ if environ is None else environ)
    if operational_release.qualification(env) == "operational":
        return operational_release.preflight(env, now=now)
    job(env)
    required = (
        "ADP_TOOLS_SMOKE_APPROVAL_FILE",
        "ADP_TOOLS_SMOKE_FIXTURE_FILE",
        "ADP_TOOLS_SMOKE_NATIVE_LOGIN_FILE",
        "ADP_TOOLS_SMOKE_DELIVERY_FILE",
        "ADP_TOOLS_SMOKE_EVIDENCE_DIR",
    )
    for name in required:
        if not env.get(name):
            raise AdmissionUnavailable(
                f"Missing {name}; deliver original native inputs before apply"
            )
    reference = ApprovedReference.read(env[required[0]], env)
    fixture = SearchFixture.read(env[required[1]], reference)
    token = private_file(env[required[2]], 16384).decode().strip()
    if not JWT.fullmatch(token):
        raise AdmissionUnavailable(
            "Dedicated native ACCESS JWT file is required; refresh tokens are unsupported"
        )
    delivery = json.loads(
        private_file(env[required[3]], 16384), object_pairs_hook=unique_object
    )
    current = time.time() if now is None else now
    if (
        not isinstance(delivery, dict)
        or delivery.get("version") != 1
        or any(
            delivery.get(name) != getattr(reference, name)
            for name in (
                "run_id",
                "github_run_id",
                "github_run_attempt",
                "source_revision",
                "definition_revision",
            )
        )
        or type(delivery.get("run_started_at")) is not int
        or type(delivery.get("expires_at")) is not int
        or not 0
        < delivery["run_started_at"]
        <= current
        < min(delivery["expires_at"], delivery["run_started_at"] + 300)
    ):
        raise AdmissionUnavailable(
            "Original native input receipt is missing, expired or belongs to another job"
        )
    return reference, fixture


def run(outputs, targets, environ=None, *, exchange=None):
    env = dict(os.environ if environ is None else environ)
    if operational_release.qualification(env) == "operational":
        return operational_release.run(outputs, targets, env)
    reference, fixture = preflight(env)
    evidence_dir = Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"])
    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence_file = evidence_dir / "native-smoke.json"
    # Refuse an existing result before exchanging or dispatching any effect.
    if evidence_file.exists() or evidence_file.is_symlink():
        raise AdmissionUnavailable(
            "Native smoke already has retained evidence; inspect the original run"
        )
    report = {
        "schema_version": "1.0",
        "run_id": reference.run_id,
        "approval_revision": reference.approval_revision,
        "source_revision": reference.source_revision,
        "definition_revision": reference.definition_revision,
        "behavioral_status": "failed",
        "funding_status": "unknown",
        "accounting_status": "unknown",
        "governance_complete": False,
        "cleanup_status": "unknown",
        "failures": ["enrollment_or_release_metadata_unconfirmed"],
    }
    try:
        values = json.loads(Path(outputs).read_text())
        gateway_url = values["gateway_url"]["value"]
        revision = target_revision(Path(targets))
        session = (exchange or NativeSession.exchange)(env)
        report = verify(session, fixture, gateway_url)
        report["target_sync_revision"] = revision
        receipt = json.loads(
            private_file(env["ADP_TOOLS_SMOKE_DELIVERY_FILE"], 16384),
            object_pairs_hook=unique_object,
        )
        if report.get("run_started_at") != receipt["run_started_at"]:
            report["failures"].append("original_approval_start_mismatch")
            report["behavioral_status"] = "failed"
            report["governance_complete"] = False
    except (AdmissionUnavailable, ValueError, TypeError, KeyError, OSError):
        report["behavioral_status"] = "failed"
        report["governance_complete"] = False
        if "enrollment_or_release_metadata_unconfirmed" not in report["failures"]:
            report["failures"].append("enrollment_or_release_metadata_unconfirmed")
    retain(evidence_file, report)
    if report["behavioral_status"] != "passed":
        raise AdmissionUnavailable(
            "Native authenticated MCP smoke failed; retained original-run evidence; release publication refused"
        )
    return {
        "tools_list_sha256": report["tools_list_sha256"],
        "target_sync_revision": report["target_sync_revision"],
        "native_run_id": reference.run_id,
        "native_evidence_sha256": hashlib.sha256(
            evidence_file.read_bytes()
        ).hexdigest(),
        "behavioral_status": report["behavioral_status"],
        "cleanup_status": report["cleanup_status"],
        "funding_status": report["funding_status"],
        "accounting_status": report["accounting_status"],
        "governance_complete": report["governance_complete"],
    }


def qualify(release_path, environ=None):
    """Qualify selected smoke only after the immutable native receipt is retained."""
    env = dict(os.environ if environ is None else environ)
    if operational_release.qualification(env) == "operational":
        return operational_release.qualify(release_path, env)
    job(env)
    directory = Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"])
    evidence = private_file(directory / "native-smoke.json", 16384)
    report = json.loads(evidence, object_pairs_hook=unique_object)
    retention = json.loads(
        private_file(directory / "retention.json", 8192),
        object_pairs_hook=unique_object,
    )
    release_path = Path(release_path)
    release = json.loads(
        private_file(release_path, 65536), object_pairs_hook=unique_object
    )
    digest = hashlib.sha256(evidence).hexdigest()
    manifest = retention.get("manifest", {})
    expected_prefix = (
        f"artifacts/14/aws-e/adp/{env['GITHUB_RUN_ID']}/1/release/native-tools-smoke/"
    )
    if (
        retention.get("version") != 1
        or retention.get("native_smoke_sha256") != digest
        or report.get("behavioral_status") != "passed"
        or report.get("cleanup_status") != "confirmed"
        or release.get("component") != "tools-platform"
        or release.get("qualification_status") != "smoke_passed_evidence_pending"
        or release.get("source_revision") != env["ADP_SOURCE_REVISION"]
        or release.get("definition_revision") != env["ADP_DEFINITION_REVISION"]
        or report.get("source_revision") != env["ADP_SOURCE_REVISION"]
        or report.get("definition_revision") != env["ADP_DEFINITION_REVISION"]
        or release.get("native_smoke", {}).get("native_evidence_sha256") != digest
        or release["native_smoke"].get("native_run_id") != report.get("run_id")
        or any(
            release["native_smoke"].get(name) != report.get(name)
            for name in (
                "behavioral_status",
                "cleanup_status",
                "funding_status",
                "accounting_status",
                "governance_complete",
            )
        )
        or not isinstance(manifest, dict)
        or not re.fullmatch(
            r"adp-dev-ci-evidence-[0-9]{12}", str(manifest.get("bucket", ""))
        )
        or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sha256", "")))
        or manifest.get("key")
        != expected_prefix + manifest.get("sha256", "") + ".manifest.json"
        or not isinstance(manifest.get("version_id"), str)
        or manifest["version_id"] in {"", "null"}
    ):
        raise AdmissionUnavailable(
            "Original successful smoke does not have confirmed retained evidence"
        )
    release["qualification_status"] = "selected_smoke_verified_evidence_retained"
    release["native_evidence_manifest"] = manifest
    replacement = release_path.with_suffix(".qualified.json")
    private_write(replacement, canonical(release) + b"\n")
    os.replace(replacement, release_path)


def retain_failure():
    """Keep a bounded stage failure even when enrollment was never attempted."""
    directory = os.environ.get("ADP_TOOLS_SMOKE_EVIDENCE_DIR")
    if not directory:
        return
    mode = operational_release.qualification(os.environ)
    path = Path(directory) / ("operational-smoke.json" if mode == "operational" else "native-smoke.json")
    if not path.exists() and not path.is_symlink():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        retain(
            path,
            {
                "schema_version": "1.0",
                "qualification_mode": mode,
                "behavioral_status": "failed",
                "governance_complete": False,
                "failures": ["native_inputs_or_preflight_unconfirmed"],
            },
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("preflight")
    smoke = commands.add_parser("smoke")
    smoke.add_argument("--outputs", type=Path, required=True)
    smoke.add_argument("--targets", type=Path, required=True)
    qualification = commands.add_parser("qualify")
    qualification.add_argument("--release", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.mode == "preflight":
        preflight()
    elif args.mode == "qualify":
        qualify(args.release)
    else:
        print(json.dumps(run(args.outputs, args.targets)))


if __name__ == "__main__":
    try:
        main()
    except AdmissionUnavailable as error:
        try:
            retain_failure()
        except (ValueError, TypeError, KeyError, OSError):
            pass
        raise SystemExit(str(error)) from None
    except (ValueError, TypeError, KeyError, OSError):
        try:
            retain_failure()
        except (ValueError, TypeError, KeyError, OSError):
            pass
        raise SystemExit(
            "Native smoke inputs are unavailable; release publication refused"
        ) from None
