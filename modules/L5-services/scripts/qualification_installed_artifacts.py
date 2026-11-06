"""Read the selected modular release around native cases without browser or pod exec.

Snapshots prove observed artifact/runtime identities. They do not prove historical
rollback, prior-schema compatibility, provider faults, or a complete UI journey.
The interface is shared by WA and Transform; callers own native-case admission.
"""

import base64
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

from l5_services.runtime_names import functions, queue_scope

from qualification_well_architected_clients import pinned_file


def canonical(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def lifecycle_module(region):
    spec = importlib.util.spec_from_file_location("l5_artifact_lifecycle", Path(__file__).with_name("worker-lifecycle.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def bounded_aws(*arguments):
        completed = subprocess.run(["aws", *arguments, "--region", region, "--output", "json", "--cli-connect-timeout", "3",
                                    "--cli-read-timeout", "10"], capture_output=True, text=True, timeout=45, check=True)
        if len(completed.stdout) > 2 * 1024 * 1024:
            raise ValueError("installed AWS readback exceeds its bound")
        return json.loads(completed.stdout) if completed.stdout.strip() else {}
    module.aws = bounded_aws
    return module


def validate_selection(config, fixture, context):
    fields = {"cluster_name", "namespace", "proof_invoke_url", "release_bucket", "frontend_assets",
              "cli_directory", "cli_files_sha256", "product_profile_enabled", "product_onboarding_enabled"}
    if not isinstance(config, dict) or set(config) != fields:
        raise ValueError("installed snapshot needs an exact protected runtime and client selection")
    region, account, environment, service = queue_scope(fixture.queue_arn)
    if (account != fixture.account_id or environment != context["environment"] or service != context["service"]
            or fixture.source_revision != context["source_revision"]
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", config["cluster_name"])
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", config["namespace"])
            or not re.fullmatch(rf"https://[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com/[A-Za-z0-9_-]+",
                                config["proof_invoke_url"])
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", config["release_bucket"])
            or any(type(config[name]) is not bool for name in ("product_profile_enabled", "product_onboarding_enabled"))):
        raise ValueError("installed snapshot target differs from the selected modular release")
    assets = config["frontend_assets"]
    if (not isinstance(assets, dict) or not 1 <= len(assets) <= 200
            or "index.html" not in assets
            or any(not isinstance(path, str) or len(path) > 256 or path.startswith("/")
                   or any(part in {"", ".", ".."} for part in path.split("/"))
                   or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
                   or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for path, digest in assets.items())
            or canonical(assets) != fixture.frontend_asset_sha256):
        raise ValueError("frontend pin must identify the complete reviewed asset digest map")
    pins = config["cli_files_sha256"]
    helpers = {"adp", "adp_deployments.py", "adp-service.py", "adp_common.py", "bg-cognito-auth.sh", "command-manifest.json"}
    if not isinstance(pins, dict) or set(pins) != helpers or pins["command-manifest.json"] != fixture.cli_manifest_sha256:
        raise ValueError("installed CLI must pin every read/auth helper and its command manifest")
    for name, digest in pins.items():
        pinned_file(str(Path(config["cli_directory"]) / name), digest, executable=name == "adp")
    return region, account, environment, service


def gateway_readback(session, config, fixture, *, runtime_class=None):
    # Existing protected IAM probe binds schema/package observations to each
    # currently serving pod. No ambient kubeconfig or arbitrary exec path.
    from src.orchestration.deployment_runtime_reader import AWS_BOUNDS, KubernetesRuntime

    parent = runtime_class or KubernetesRuntime

    class RecordingRuntime(parent):
        def schema(self, pod, *, product=False):
            value = super().schema(pod, product=product)
            if (value.get("l5_package_version") != fixture.package_version
                    or value.get("l5_wheel_sha256") != fixture.wheel_sha256
                    or value.get("l5_schema_sha256") != fixture.schema_sha256):
                raise ValueError("serving gateway package/schema differs from the reviewed release")
            self.proofs.append(value)
            return value

    description = session.client("eks", config=AWS_BOUNDS).describe_cluster(name=config["cluster_name"])["cluster"]
    expected = f"arn:aws:eks:{session.region_name}:{fixture.account_id}:cluster/{config['cluster_name']}"
    if description.get("arn") != expected or description.get("status") != "ACTIVE":
        raise ValueError("selected gateway cluster identity changed")
    runtime = RecordingRuntime(session, description, config["namespace"], proof_invoke_url=config["proof_invoke_url"])
    runtime.proofs = []
    try:
        before = runtime.get(f"/apis/apps/v1/namespaces/{config['namespace']}/deployments/bedrockgateway")["metadata"]
        product = SimpleNamespace(product_profile_enabled=config["product_profile_enabled"],
                                  product_onboarding_enabled=config["product_onboarding_enabled"],
                                  cli_manifest_sha256=fixture.cli_manifest_sha256, migration_head=fixture.database_head)
        uids, head = runtime.gateway(digest="sha256:" + fixture.gateway_image_digest, source_revision=fixture.source_revision,
                                    account=fixture.account_id, region=session.region_name, product=product,
                                    wheel_sha256=fixture.wheel_sha256)
        deployment = runtime.get(f"/apis/apps/v1/namespaces/{config['namespace']}/deployments/bedrockgateway")
        if (not runtime.proofs or sorted(proof["pod_uid"] for proof in runtime.proofs) != uids
                or any(deployment["metadata"].get(key) != before.get(key) for key in ("uid", "generation"))):
            raise ValueError("installed schema proof does not cover every serving gateway")
        return {"pod_uids": uids, "migration_head": head,
                "deployment_uid": deployment["metadata"]["uid"], "deployment_generation": deployment["metadata"]["generation"],
                "proofs": runtime.proofs}
    finally:
        runtime.close()


def frontend_readback(session, config, fixture, context, *, reader=None):
    from src.orchestration.deployment_runtime_reader import PilotRuntimeReader

    value = SimpleNamespace(account_id=fixture.account_id, assets=config["frontend_assets"])
    count = (reader or PilotRuntimeReader()).frontend(session, value, context["environment"])
    if count != len(config["frontend_assets"]):
        raise ValueError("served frontend asset readback incomplete")
    return {"assets": config["frontend_assets"], "asset_map_sha256": canonical(config["frontend_assets"])}


def _collect(config, fixture, context, *, session=None, lifecycle=None, read_gateway=gateway_readback,
             read_frontend=frontend_readback):
    """Execute bounded reads. Lambda probe only loads the factory; it admits no work."""
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    region, account, environment, service = validate_selection(config, fixture, context)
    session = session or boto3.Session(region_name=region)
    if session.region_name != region:
        raise ValueError("installed snapshot session belongs to another region")
    lifecycle = lifecycle or lifecycle_module(region)
    lifecycle.identity(account, fixture.role_arn)
    principal = session.client("sts", config=Config(connect_timeout=3, read_timeout=10,
                          retries={"total_max_attempts": 1})).get_caller_identity()
    expected = f"arn:aws:sts::{account}:assumed-role/{fixture.role_arn.rsplit('/', 1)[-1]}/"
    if principal.get("Account") != account or not principal.get("Arn", "").startswith(expected):
        raise ValueError("artifact and runtime readers do not share the exact selected role")
    _, archive_version, code_hash = lifecycle.manifest_for(config["release_bucket"], fixture.source_revision,
                                                         fixture.wheel_sha256, fixture.manifest_version)
    if code_hash != fixture.worker_code_sha256:
        raise ValueError("immutable worker archive differs from the selected release")
    worker, repair = functions(environment, service)
    uuid = lifecycle.mapping(worker, environment, account, service)
    observed = lifecycle.state(environment, worker, repair, uuid)
    lifecycle.scope_readback(environment, account, worker, repair, observed, service)
    names = lifecycle.auxiliary_names(environment, service)
    auxiliary = lifecycle.auxiliary_state(names, observed, account)
    functions_observed = {worker: observed["worker"], repair: observed["repair"],
                          **{name: value["function"] for name, value in auxiliary.items()}}
    client = session.client("lambda", config=Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1}))
    revisions = {}
    for name, fn in functions_observed.items():
        if (fn.get("CodeSha256") != code_hash or fn.get("State") != "Active"
                or fn.get("LastUpdateStatus") != "Successful" or not fn.get("RevisionId")):
            raise ValueError("selected installed worker is unhealthy or differs from the immutable archive")
        try:
            client.get_function_url_config(FunctionName=name)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise ValueError("public function URL absence could not be verified") from None
        else:
            raise ValueError("selected L5 function has a public URL")
        revisions[name] = {"revision_id": fn["RevisionId"], "code_sha256": fn["CodeSha256"],
                           "configuration_sha256": canonical(fn.get("Environment", {}).get("Variables", {}))}
    lifecycle.verify_runtime(worker, repair, service, names)
    gateway = read_gateway(session, config, fixture)
    frontend = read_frontend(session, config, fixture, context)
    # Do not accept evidence racing a worker rollout or gateway rollout.
    current = lifecycle.state(environment, worker, repair, uuid)
    current_auxiliary = lifecycle.auxiliary_state(names, current, account)
    if (any(current[label].get("RevisionId") != observed[label].get("RevisionId") for label in ("worker", "repair"))
            or any(current_auxiliary[name]["function"].get("RevisionId") != revisions[name]["revision_id"] for name in names)
            or current["mapping"] != observed["mapping"] or current["rule"] != observed["rule"]):
        raise ValueError("worker changed during installed snapshot collection")
    validate_selection(config, fixture, context)
    lifecycle.identity(account, fixture.role_arn)
    return {"schema_version": 1, "source": "independent-installed-read", "observed_at": datetime.now(UTC).isoformat(),
            "target": {"account_id": account, "region": region, "role_arn": fixture.role_arn,
                       "service": service, "environment": environment},
            "source_revision": fixture.source_revision, "manifest_version": fixture.manifest_version,
            "archive_version": archive_version, "functions": revisions,
            "mapping": {"uuid": uuid, "state": observed["mapping"], "queue_arn": fixture.queue_arn,
                        "repair_state": observed["rule"]}, "gateway": gateway, "frontend": frontend,
            "cli_files_sha256": dict(config["cli_files_sha256"]),
            "digests": {"wheel": fixture.wheel_sha256, "worker": base64.b64decode(code_hash, validate=True).hex(),
                        "schema": fixture.schema_sha256, "gateway": fixture.gateway_image_digest,
                        "frontend": frontend["asset_map_sha256"], "cli": fixture.cli_manifest_sha256},
            "limitations": ["prior_schema_and_rollback_not_observed", "client_journey_not_proved"]}


def collect(config, fixture, context, **readers):
    import httpx
    from botocore.exceptions import BotoCoreError, ClientError
    from src.orchestration.review_cycle import CycleBlockedError

    try:
        return _collect(config, fixture, context, **readers)
    except (BotoCoreError, ClientError, httpx.HTTPError, CycleBlockedError, subprocess.SubprocessError):
        # Provider responses can contain private target information. The release
        # caller supplies the source-controlled stage and exact recovery action.
        raise ValueError("protected installed readback unavailable; verify selected runtime-reader authority") from None


def stable_identity(snapshot):
    return {key: snapshot[key] for key in ("target", "source_revision", "manifest_version", "archive_version",
                                           "functions", "digests", "cli_files_sha256")} | {
        "gateway": {key: snapshot["gateway"][key] for key in ("deployment_uid", "deployment_generation", "migration_head")},
        "mapping": snapshot["mapping"], "frontend": snapshot["frontend"],
    }


class SnapshotSeries:
    """Fsynced private checkpoints cannot rebaseline an interrupted native run."""

    def __init__(self, directory, config, fixture, context, driver, *, collector=collect):
        self.directory, self.config, self.fixture, self.context, self.driver = directory, config, fixture, context, driver
        self.collector = collector
        validate_selection(config, fixture, context)

    def read(self, stage):
        record = self.driver.private_json(self.directory / ("installed-" + stage + ".json"))
        if (record.get("context") != self.context or record.get("selection_sha256") != canonical(self.config)
                or record.get("stage") != stage or record.get("observation_sha256") != canonical(record["observation"])
                or record["observation"].get("source") != "independent-installed-read"):
            raise ValueError("installed checkpoint belongs to another release or selection")
        when = datetime.fromisoformat(record["observation"]["observed_at"])
        if when.tzinfo is None or not datetime.now(UTC) - timedelta(hours=24) <= when <= datetime.now(UTC) + timedelta(seconds=30):
            raise ValueError("installed checkpoint expired; do not retrospectively rebaseline native work")
        return record

    def checkpoint(self, stage, *, completed_case=None):
        if stage not in {"before-native-cases", "after-native-case-1", "after-native-case-2"}:
            raise ValueError("unknown installed checkpoint stage")
        path = self.directory / ("installed-" + stage + ".json")
        if path.exists():
            retained = self.read(stage)
            if retained.get("completed_case") != completed_case:
                raise ValueError("installed checkpoint belongs to different Product work")
            return retained
        journal = self.directory / "bootstrap-journal"
        if stage == "before-native-cases" and journal.exists() and (journal.is_symlink() or any(journal.iterdir())):
            raise ValueError("cannot create a retrospective installed baseline after Product intent was retained")
        previous = None if stage == "before-native-cases" else self.read(
            "before-native-cases" if stage == "after-native-case-1" else "after-native-case-1")
        observation = self.collector(self.config, self.fixture, self.context)
        if previous and stable_identity(observation) != stable_identity(previous["observation"]):
            raise ValueError("installed artifacts or deployment revisions changed between selected native cases")
        record = {"context": self.context, "selection_sha256": canonical(self.config), "stage": stage,
                  "previous_sha256": canonical(previous) if previous else None, "completed_case": completed_case,
                  "observation_sha256": canonical(observation), "observation": observation}
        self.driver.write_private(path, record)
        return record

    def verify(self, cases):
        if not isinstance(cases, list) or len(cases) != 2:
            raise ValueError("installed comparison requires exactly two native cases")
        records = [self.read(stage) for stage in ("before-native-cases", "after-native-case-1", "after-native-case-2")]
        for index, case in enumerate(cases, 1):
            expected = case_identity(case)
            if (records[index]["completed_case"] != expected
                    or records[index]["previous_sha256"] != canonical(records[index - 1])
                    or stable_identity(records[index]["observation"]) != stable_identity(records[0]["observation"])):
                raise ValueError("installed snapshots are not bound to both selected native cases")
        current = self.collector(self.config, self.fixture, self.context)
        if stable_identity(current) != stable_identity(records[0]["observation"]):
            raise ValueError("installed release changed after the native cases")
        return {"source": "independent-installed-read", "source_revision": self.context["source_revision"],
                "digests": current["digests"], "snapshot_sha256": [canonical(record) for record in records],
                "case_identities": [record["completed_case"] for record in records[1:]],
                "migration_head": current["gateway"]["migration_head"],
                "observation_interval": {"start": records[0]["observation"]["observed_at"], "end": current["observed_at"]},
                "limitations": current["limitations"]}


def case_identity(case):
    scope = case["result"]["scope"] if "result" in case else case["scope"]
    return {"scope": {key: scope[key] for key in ("tenant_id", "product_id", "project_id")},
            "run_id": case["run_id"], "result_revision": case["result_revision"]}
