"""Retain real old-service readbacks across an installed Gateway transition.

This collector never deploys, restarts, submits work or invokes a Lambda. A
changed serving pod is an observation, not proof that a particular recovery
operation ran. Full WA07/AC-01 and schema-compatible remain incomplete.
"""

import argparse
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import quote

import httpx

from l5_services.installed_release_probe import InstalledFixture
import qualification_installed_artifacts as artifacts
import qualification_well_architected as driver
from qualification_well_architected_journal import Journal, encoded


STAGES = ("before-transition", "after-transition", "after-recovery")
LEGACY_LIMITATIONS = ["rollback_operation_not_observed", "devops_durable_recovery_rows_not_exposed",
              "old_cli_mcp_and_ui_callers_not_exercised", "worker_transport_and_repair_not_observed",
              "product_origin_not_bound_to_gateway_probe", "native_acceptance_not_evaluated"]
LIMITATIONS = [value for value in LEGACY_LIMITATIONS if value != "devops_durable_recovery_rows_not_exposed"]
PRODUCT_READ_BYTES = 64 * 1024
DEVOPS_RECOVERY_BYTES = 8 * 1024 * 1024
# Bound each response using the export's compact ASCII-escaped encoding as well
# as wire bytes, so Journal cannot amplify Unicode past the retained byte limit.
# One 8MiB export plus the separately 64KiB-bounded records fits within 9MiB.
CHECKPOINT_BYTES = DEVOPS_RECOVERY_BYTES + 16 * PRODUCT_READ_BYTES
CHECKPOINT_CONTRACT = "wa-installed-compatibility-checkpoint/v2"
LEGACY_CHECKPOINT_CONTRACT = "wa-installed-compatibility-checkpoint/v1"
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")


class LegacyRecoveryMissing(ValueError):
    """Original observations cannot be supplemented retroactively."""


def instant(value):
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("compatibility evidence needs timezone-aware timestamps")
    return moment


def validate(selection):
    if (not isinstance(selection, dict) or set(selection) != {
            "schema_version", "source_revision", "environment", "expires_at", "records", "stages"}
            or selection["schema_version"] != 1
            or not re.fullmatch(r"[a-f0-9]{40}", selection["source_revision"])
            or selection["environment"] not in {"dev", "test", "prod"}
            or not datetime.now(UTC) < instant(selection["expires_at"]) <= datetime.now(UTC) + timedelta(hours=24)
            or not isinstance(selection["records"], list) or len(selection["records"]) != 2
            or {item.get("service_id") for item in selection["records"]} != {"security", "devops"}
            or set(selection["stages"]) != set(STAGES)):
        raise ValueError("select exactly one original Security and DevOps record and three bounded stages")
    for item in selection["records"]:
        if (set(item) != {"service_id", "gateway_url", "token_env", "scope", "run_id", "result_id", "result_revision"}
                or set(item["scope"]) != {"tenant_id", "product_id", "project_id"}
                or any(not isinstance(value, str) or not IDENTIFIER.fullmatch(value)
                       for value in [item["run_id"], item["result_id"], *item["scope"].values()])
                or type(item["result_revision"]) is not int or item["result_revision"] < 1
                or not driver.TOKEN_ENV.fullmatch(item["token_env"])):
            raise ValueError("compatibility selection must pin existing scoped run and result identities")
        url = httpx.URL(item["gateway_url"])
        if url.scheme != "https" or not url.host or url.path != "/" or url.userinfo or url.query or url.fragment:
            raise ValueError("compatibility selection requires an exact HTTPS Product origin")
    if len({item["gateway_url"] for item in selection["records"]}) != 1:
        raise ValueError("both old services must use the same selected Product origin")
    for stage in STAGES:
        config = selection["stages"][stage]
        if (not isinstance(config, dict) or set(config) != {"fixture_path", "fixture_sha256", "runtime"}
                or not re.fullmatch(r"[a-f0-9]{64}", config["fixture_sha256"])):
            raise ValueError("each compatibility stage needs a pinned private installed fixture")


def fixture_for(selection, stage):
    selected = selection["stages"][stage]
    driver.private_json(selected["fixture_path"])
    path = Path(selected["fixture_path"])
    if sha256(path.read_bytes()).hexdigest() != selected["fixture_sha256"]:
        raise ValueError("compatibility installed fixture changed")
    fixture = InstalledFixture.from_manifest(path)
    context = {"source_revision": fixture.source_revision, "environment": selection["environment"],
               "service": "well_architected"}
    artifacts.validate_selection(selected["runtime"], fixture, context)
    if stage != STAGES[0] and fixture.source_revision != selection["source_revision"]:
        raise ValueError("compatibility stage is not the selected release")
    if stage == STAGES[0] and fixture.source_revision == selection["source_revision"]:
        raise ValueError("cannot manufacture an old baseline after the selected release was installed")
    return fixture


def _runtime_read(selection, stage):
    """Only STS, EKS/Kubernetes GETs and the protected schema GET; no factory probe."""
    import boto3
    from src.orchestration.deployment_runtime_reader import AWS_BOUNDS

    fixture = fixture_for(selection, stage)
    region, account, _, _ = artifacts.queue_scope(fixture.queue_arn)
    session = boto3.Session(region_name=region)
    identity = session.client("sts", config=AWS_BOUNDS).get_caller_identity()
    prefix = f"arn:aws:sts::{account}:assumed-role/{fixture.role_arn.rsplit('/', 1)[-1]}/"
    if identity.get("Account") != account or not identity.get("Arn", "").startswith(prefix):
        raise ValueError("compatibility readback needs the selected runtime-reader role")
    gateway = artifacts.gateway_readback(session, selection["stages"][stage]["runtime"], fixture)
    return {"source": "protected-installed-gateway-read", "account_id": account, "region": region, "role_arn": fixture.role_arn,
            "source_revision": fixture.source_revision, "schema_sha256": fixture.schema_sha256,
            "wheel_sha256": fixture.wheel_sha256, "gateway_image_digest": fixture.gateway_image_digest,
            "gateway": {key: gateway[key] for key in (
                "pod_uids", "migration_head", "deployment_uid", "deployment_generation")},
            "proofs_sha256": artifacts.canonical(gateway["proofs"])}


def runtime_read(selection, stage):
    from botocore.exceptions import BotoCoreError, ClientError
    from src.orchestration.review_cycle import CycleBlockedError

    try:
        return _runtime_read(selection, stage)
    except (BotoCoreError, ClientError, httpx.HTTPError, CycleBlockedError):
        raise ValueError("protected compatibility runtime read unavailable; verify the selected reader authority") from None


def stable_runtime(value):
    return {key: item for key, item in value.items() if key != "proofs_sha256"}


def get(client, path, *, max_bytes=PRODUCT_READ_BYTES):
    with client.stream("GET", path) as response:
        response.raise_for_status()
        raw = bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw) > max_bytes:
                raise ValueError("compatibility Product readback exceeds its bound")
        value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("compatibility Product readback must be an object")
    if len(encoded(value)) > max_bytes:
        raise ValueError("compatibility Product readback exceeds its retained byte bound")
    return value


def validate_record(record, selected, *, legacy=False):
    if not isinstance(record, dict):
        raise ValueError("compatibility record must be an object")
    scope = selected["scope"]
    actor, run, result = (record.get(key) for key in ("actor", "run", "result"))
    if (record.get("service_id") != selected["service_id"] or record.get("scope") != scope
            or any(not isinstance(value, dict) for value in (actor, run, result))
            or actor.get("account_type") != "human" or actor.get("org_id") != scope["tenant_id"]
            or not isinstance(actor.get("user_id"), str) or not actor["user_id"]
            or run.get("run_id") != selected["run_id"] or result.get("run_id") != selected["run_id"]
            or result.get("result_id") != selected["result_id"]
            or type(result.get("revision")) is not int or result["revision"] != selected["result_revision"]
            or type(run.get("revision")) is not int or run["revision"] < 1
            or any(not isinstance(document.get("scope"), dict)
                   or any(document["scope"].get(key) != value for key, value in scope.items())
                   for document in (run, result))):
        raise ValueError("compatibility API returned another reader, run, result, revision or scope")
    # /auth/me retains the login subject; L5 resolves the canonical workspace
    # owner server-side. Keep the raw actor bookends without equating namespaces.
    owner_id = run["scope"].get("owner_id")
    if not isinstance(owner_id, str) or not owner_id or result["scope"].get("owner_id") != owner_id:
        raise ValueError("compatibility run and result belong to different owners")
    recovery = record.get("recovery")
    if legacy and selected["service_id"] == "devops" and recovery is None:
        return  # An original absence stays absent; never fill historical facts.
    # Security admission validates its optional default without materializing
    # it in the stored input. Only absence means Security; explicit null/wrong
    # values still fail, and an absent DevOps service cannot match DevOps.
    if not isinstance(run.get("input"), dict) or run["input"].get("service_id", "security") != selected["service_id"]:
        raise ValueError("compatibility recovery belongs to another service")
    if (not isinstance(recovery, dict) or set(recovery) != {"run", "attempts", "work", "publications"}
            or recovery["run"] != run or not recovery["attempts"] or not recovery["work"]
            or any(not isinstance(recovery[field], list)
                   or any(not isinstance(row, dict) for row in recovery[field])
                   for field in ("attempts", "work", "publications"))):
        raise ValueError("compatibility recovery readback lacks the original run, attempts or work")


def records_read(selection, connection=driver.product_connection):
    records = []
    for item in selection["records"]:
        scope = item["scope"]
        path = "/api/v1/products/{}/projects/{}/services/{}".format(
            quote(scope["product_id"], safe=""), quote(scope["project_id"], safe=""), item["service_id"])
        with connection(item) as client:
            owner = get(client, "/api/auth/me")
            actor = {key: owner.get(key) for key in ("user_id", "org_id", "account_type")}
            if (actor["account_type"] != "human" or actor["org_id"] != scope["tenant_id"]
                    or not isinstance(actor["user_id"], str) or not actor["user_id"]):
                raise ValueError("compatibility reader is not the selected tenant's human")
            run_path = path + "/runs/" + quote(item["run_id"], safe="")
            result_path = path + "/results/" + quote(item["result_id"], safe="")
            run, result = get(client, run_path), get(client, result_path)
            recovery_bytes = DEVOPS_RECOVERY_BYTES if item["service_id"] == "devops" else PRODUCT_READ_BYTES
            recovery = get(client, run_path + "/recovery-state", max_bytes=recovery_bytes)
            record = {"service_id": item["service_id"], "scope": scope, "actor": actor,
                      "run": run, "result": result, "recovery": recovery}
            validate_record(record, item)
            current_owner = get(client, "/api/auth/me")
            if (get(client, run_path) != run or get(client, result_path) != result
                    or get(client, run_path + "/recovery-state", max_bytes=recovery_bytes) != recovery
                    or {key: current_owner.get(key) for key in actor} != actor):
                raise ValueError("compatibility records or reader changed during collection")
        records.append(record)
    return records


class CompatibilitySeries:
    def __init__(self, directory, selection, *, read_runtime=runtime_read, read_records=records_read):
        validate(selection)
        self.selection, self.read_runtime, self.read_records = selection, read_runtime, read_records
        self.journal = Journal(directory, {"contract": "wa-installed-compatibility/v1", "selection": selection},
                               max_bytes=CHECKPOINT_BYTES)

    def read(self, stage):
        value = self.journal.read(stage + ".json")
        if value is None:
            raise ValueError("missing original compatibility checkpoint; do not backfill historical evidence")
        if (value.get("contract") not in {CHECKPOINT_CONTRACT, LEGACY_CHECKPOINT_CONTRACT} or value.get("stage") != stage
                or value.get("selection_sha256") != artifacts.canonical(self.selection)
                or value.get("snapshot_sha256") != artifacts.canonical(value["snapshot"])):
            raise ValueError("compatibility checkpoint differs from its original selection or facts")
        started, observed = instant(value["read_started_at"]), instant(value["observed_at"])
        now = datetime.now(UTC)
        if not now - timedelta(hours=24) <= started <= observed <= now + timedelta(seconds=30):
            raise ValueError("compatibility checkpoint is historical or has an invalid interval")
        self.validate_snapshot(value["snapshot"], stage, legacy=value["contract"] == LEGACY_CHECKPOINT_CONTRACT)
        return value

    def validate_snapshot(self, snapshot, stage, *, legacy=False):
        fixture = fixture_for(self.selection, stage)
        runtime = snapshot["runtime"]
        region, account, _, _ = artifacts.queue_scope(fixture.queue_arn)
        expected = {"source": "protected-installed-gateway-read", "account_id": account, "region": region,
                    "role_arn": fixture.role_arn, "source_revision": fixture.source_revision,
                    "schema_sha256": fixture.schema_sha256, "wheel_sha256": fixture.wheel_sha256,
                    "gateway_image_digest": fixture.gateway_image_digest}
        gateway = runtime["gateway"]
        if (any(runtime.get(key) != value for key, value in expected.items())
                or gateway.get("migration_head") != fixture.database_head
                or not isinstance(gateway.get("pod_uids"), list) or not gateway["pod_uids"]
                or len(set(gateway["pod_uids"])) != len(gateway["pod_uids"])
                or any(not isinstance(uid, str) or not uid for uid in gateway["pod_uids"])
                or not isinstance(gateway.get("deployment_uid"), str) or not gateway["deployment_uid"]
                or type(gateway.get("deployment_generation")) is not int or gateway["deployment_generation"] < 1
                or not re.fullmatch(r"[a-f0-9]{64}", runtime.get("proofs_sha256", ""))
                or not isinstance(snapshot["records"], list) or len(snapshot["records"]) != 2):
            raise ValueError("compatibility snapshot differs from the pinned installed target")
        for record, selected in zip(snapshot["records"], self.selection["records"], strict=True):
            validate_record(record, selected, legacy=legacy)

    def snapshot(self, stage):
        before = self.read_runtime(self.selection, stage)
        records = self.read_records(self.selection)
        after = self.read_runtime(self.selection, stage)
        if stable_runtime(before) != stable_runtime(after):
            raise ValueError("installed Gateway changed during compatibility readback")
        value = {"runtime": after, "records": records}
        self.validate_snapshot(value, stage)
        return value

    def checkpoint(self, stage):
        if stage not in STAGES:
            raise ValueError("unknown compatibility checkpoint stage")
        if self.journal.read(stage + ".json") is not None:
            return self.read(stage)  # never replace an earlier observation
        index = STAGES.index(stage)
        prior = self.read(STAGES[index - 1]) if index else None
        if any(self.journal.read(later + ".json") is not None for later in STAGES[index + 1:]):
            raise ValueError("cannot insert a retrospective compatibility baseline")
        started = datetime.now(UTC).isoformat()
        snapshot = self.snapshot(stage)
        if prior and instant(prior["observed_at"]) >= instant(started):
            raise ValueError("compatibility checkpoint intervals overlap")
        value = {"contract": CHECKPOINT_CONTRACT, "stage": stage,
                 "selection_sha256": artifacts.canonical(self.selection), "read_started_at": started,
                 "observed_at": datetime.now(UTC).isoformat(), "previous_sha256": artifacts.canonical(prior) if prior else None,
                 "snapshot": snapshot, "snapshot_sha256": artifacts.canonical(snapshot)}
        self.journal.retain(stage + ".json", value)
        return value

    def verify(self):
        values = [self.read(stage) for stage in STAGES]
        for index, value in enumerate(values):
            prior = values[index - 1] if index else None
            if (value["previous_sha256"] != (artifacts.canonical(prior) if prior else None)
                    or prior and instant(prior["observed_at"]) >= instant(value["read_started_at"])):
                raise ValueError("compatibility checkpoints do not form the original ordered chain")
        snapshots = [value["snapshot"] for value in values]
        if any(snapshot["records"] != snapshots[0]["records"] for snapshot in snapshots[1:]):
            raise ValueError("old public records, recovery state or caller identity changed")
        if any(record["service_id"] == "devops" and record.get("recovery") is None
               for snapshot in snapshots for record in snapshot["records"]):
            raise LegacyRecoveryMissing("compatibility remains incomplete: original DevOps recovery was not collected; retain legacy checkpoints")
        runtimes = [snapshot["runtime"] for snapshot in snapshots]
        if (runtimes[0]["source_revision"] == self.selection["source_revision"]
                or any(runtime["source_revision"] != self.selection["source_revision"] for runtime in runtimes[1:])
                or any((runtime["account_id"], runtime["region"], runtime["gateway"]["deployment_uid"])
                       != (runtimes[0]["account_id"], runtimes[0]["region"], runtimes[0]["gateway"]["deployment_uid"])
                       for runtime in runtimes[1:])):
            raise ValueError("compatibility transition crosses targets or lacks the selected release")
        current = self.snapshot(STAGES[-1])
        if stable_runtime(runtimes[-1]) != stable_runtime(current["runtime"]):
            raise ValueError("installed Gateway changed after compatibility checkpoints")
        # Fresh authorized reads must still expose the same original records.
        if current["records"] != snapshots[0]["records"]:
            raise ValueError("retained compatibility records changed after the observation")
        replaced = set(runtimes[1]["gateway"]["pod_uids"]).isdisjoint(runtimes[2]["gateway"]["pod_uids"])
        return {"contract": "wa-installed-compatibility-progress/v1", "source_revision": self.selection["source_revision"],
                "environment": self.selection["environment"], "id": "WA07/AC-01", "scenario": "WA07/SCN-01",
                "board_tasks": ["AC-01-t2", "SCN-01-t1"], "outcome": "incomplete", "native_qualified": False,
                "old_records_retained": True, "gateway_pods_replaced": replaced,
                "checkpoint_sha256": [artifacts.canonical(value) for value in values],
                "migration_heads": [runtime["gateway"]["migration_head"] for runtime in runtimes],
                "current_runtime": stable_runtime(runtimes[-1]),
                "limitations": LIMITATIONS, "observed_at": datetime.now(UTC).isoformat()}


def read_progress(directory, context, installed):
    """Re-read the retained selection and current facts during WA emission."""
    path = directory / "compatibility-checkpoints"
    selected = driver.private_json(path / "context.json")["context"]
    if selected.get("contract") != "wa-installed-compatibility/v1":
        raise ValueError("compatibility evidence directory belongs to another collector")
    selection = selected["selection"]
    if (selection["source_revision"] != context["source_revision"] or selection["environment"] != context["environment"]
            or not isinstance(installed, dict) or installed.get("source") != "independent-installed-read"
            or installed.get("source_revision") != context["source_revision"]):
        raise ValueError("compatibility progress is not bound to this installed WA release")
    report = CompatibilitySeries(path, selection).verify()
    runtime = report["current_runtime"]
    if (runtime["gateway"]["migration_head"] != installed.get("migration_head")
            or any(runtime[field] != installed.get("digests", {}).get(name) for field, name in (
                ("schema_sha256", "schema"), ("wheel_sha256", "wheel"), ("gateway_image_digest", "gateway")))):
        raise ValueError("compatibility records were read through different installed artifacts")
    return report


def main(argv=None):
    from botocore.exceptions import BotoCoreError, ClientError
    from src.orchestration.review_cycle import CycleBlockedError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=(*STAGES, "verify"))
    parser.add_argument("selection", type=Path)
    parser.add_argument("evidence_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        series = CompatibilitySeries(args.evidence_dir, driver.private_json(args.selection))
        if args.action == "verify":
            report = series.verify()
            driver.write_private(driver.private_output(args.evidence_dir / "compatibility-progress.json"), report)
        else:
            series.checkpoint(args.action)
        print("Original compatibility readback retained privately; WA07 acceptance remains incomplete")
        return 0
    except LegacyRecoveryMissing:
        raise SystemExit("Compatibility remains incomplete: the original checkpoints did not collect DevOps recovery. "
                         "Retain that journal; later observations cannot fill its historical gap or establish WA07 acceptance.") from None
    except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError, BotoCoreError, ClientError, CycleBlockedError):
        raise SystemExit("Compatibility readback unavailable or changed. Retain original checkpoints; verify scoped reader authority, exact installed pins and unchanged old records. Never backfill a baseline or restart native work.") from None


if __name__ == "__main__":
    main()
