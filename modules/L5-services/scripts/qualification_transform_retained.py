"""Read selected Transform cleanup, retention and durable progress without effects.

Only the original result evaluator calls this collector. Kubernetes reads request
metadata only; neither provider credentials nor Secret/ConfigMap data are read.
Current SQL rows cannot substitute for a fault trace or a native-effect ledger.
"""

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json
import re
from urllib.parse import quote

from sqlalchemy import select

from qualification_transform_journal import digest


COLLECTION_ACTIONS = {
    "selected-original-case": "Reconcile the selected result identity with the original native evaluator.",
    "durable-progress": "Restore read access to the original run, attempt, work and publication records.",
    "installed-cleanup-target": "Verify the pinned worker revision and read access to its exact EKS cluster and namespaces.",
    "retained-checkpoints": "Restore the original versioned provider and check checkpoints; do not replace the run.",
    "owned-resource-absence": "Reconcile remaining resources by their original receipt UID and retry metadata-only reads.",
    "retention-after-cleanup": "Restore current Product source/artifact authorization and the original retained versions; do not resubmit native work.",
}


def installed_inventory(selection, release, context, artifact_bucket):
    """Discover the actual selected worker's cluster and namespaces, with pins."""
    import boto3
    from botocore.config import Config
    from l5_services.adapters.transform.kubernetes import TransformKubernetesAPI
    from l5_services.runtime_names import functions, queue_scope

    region, account, environment, service = queue_scope(release.queue_arn)
    if (service != "transform" or account != release.account_id
            or environment != context["environment"] or release.source_revision != context["source_revision"]):
        raise ValueError("Transform cleanup target differs from selected release")
    bounds = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1})
    session = boto3.Session(region_name=region)
    principal = session.client("sts", config=bounds).get_caller_identity()
    role = f"arn:aws:sts::{account}:assumed-role/{release.role_arn.rsplit('/', 1)[-1]}/"
    if principal.get("Account") != account or not principal.get("Arn", "").startswith(role):
        raise ValueError("Transform cleanup reader differs from selected role")
    worker, _ = functions(environment, service)
    configuration = session.client("lambda", config=bounds).get_function_configuration(FunctionName=worker)
    if (configuration.get("CodeSha256") != release.worker_code_sha256
            or configuration.get("State") != "Active" or configuration.get("LastUpdateStatus") != "Successful"
            or not configuration.get("RevisionId")
            or configuration.get("FunctionArn") != f"arn:aws:lambda:{region}:{account}:function:{worker}"):
        raise ValueError("Transform cleanup reader cannot bind installed worker revision")
    values = configuration.get("Environment", {}).get("Variables", {})
    cluster = session.client("eks", config=bounds).describe_cluster(name=selection["cluster_name"])["cluster"]
    if (cluster.get("arn") != f"arn:aws:eks:{region}:{account}:cluster/{selection['cluster_name']}"
            or cluster.get("status") != "ACTIVE"
            or values.get("L5_TRANSFORM_CLUSTER_NAME") != selection["cluster_name"]
            or values.get("L5_TRANSFORM_CLUSTER_ENDPOINT") != cluster.get("endpoint")
            or values.get("L5_TRANSFORM_CLUSTER_CA") != cluster.get("certificateAuthority", {}).get("data")
            or values.get("L5_TRANSFORM_ARTIFACT_BUCKET") != artifact_bucket
            or values.get("AWS_REGION", region) != region):
        raise ValueError("Transform cleanup worker and cluster selection differ")
    namespaces = {kind: values.get(key) for kind, key in (
        ("provider", "L5_TRANSFORM_PROVIDER_NAMESPACE"), ("check", "L5_TRANSFORM_CHECK_NAMESPACE"))}
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value)
           for value in namespaces.values()):
        raise ValueError("Transform installed cleanup namespace is unavailable")
    api = TransformKubernetesAPI(endpoint=cluster["endpoint"], cluster=selection["cluster_name"],
                                 ca=cluster["certificateAuthority"]["data"], region=region)
    return api, namespaces, {"worker_revision": configuration["RevisionId"],
                             "worker_code_sha256": configuration["CodeSha256"], "cluster_arn": cluster["arn"],
                             "namespaces": namespaces}


async def read_history(sessions, run, attempt, result):
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.models.work import Work
    from l5_services.models.publication import Publication

    async with sessions() as session:
        attempts = list((await session.scalars(select(WorkerAttempt).where(
            WorkerAttempt.tenant_id == run.tenant_id, WorkerAttempt.run_id == run.run_id).limit(21))).all())
        work = list((await session.scalars(select(Work).where(
            Work.tenant_id == run.tenant_id, Work.run_id == run.run_id).order_by(
                Work.generation, Work.step, Work.work_id).limit(501))).all())
        publications = list((await session.scalars(select(Publication).where(
            Publication.tenant_id == run.tenant_id, Publication.result_id == result["result_id"],
            Publication.result_version == result["revision"]).limit(21))).all())
    if len(attempts) > 20 or len(work) > 500 or len(publications) > 20:
        raise ValueError("Transform durable readback exceeds its bounded original history")
    selected = [item for item in attempts if item.attempt_id == attempt.attempt_id]
    if (len(selected) != 1 or selected[0].native_job_ref != run.native_job_ref
            or selected[0].input_hash != attempt.input_hash
            or selected[0].idempotency_token != attempt.idempotency_token):
        raise ValueError("Transform original attempt changed during durable readback")

    def fields(value, names):
        return {key: getattr(value, key) for key in names}

    return {"source": "installed-sql-read", "run_id": run.run_id, "scope": run.admission["scope"],
            "attempts": [fields(value, ("attempt_id", "operation", "first_work_id", "first_fence",
                "input_hash", "started_at", "status", "reconcile_checks", "native_job_ref")) for value in attempts],
            "work": [fields(value, ("work_id", "kind", "generation", "step", "fence", "attempts",
                "due_at", "completed_at", "blocked_at")) for value in work],
            "publications": [fields(value, ("publication_id", "result_id", "result_version", "destination",
                "state", "revision", "contribution_hash", "receipt")) for value in publications],
            "missing": ["bounded-original-fault-trace", "independent-native-effect-ledger",
                        "later-entitled-consumer-task", "foreign-and-revoked-reuse-denials"]}


async def read_absence(api, namespaces, checkpoints, authority):
    observed = []
    seen = set()
    for entry in checkpoints:
        snapshot = entry["checkpoint"]["cleanup"]
        namespace, name = snapshot.get("namespace"), snapshot.get("name")
        if (namespace != namespaces[entry["kind"]] or not isinstance(name, str)
                or not re.fullmatch(r"tf-(?:check-)?[a-f0-9]{40}", name)
                or not isinstance(snapshot.get("resources"), list) or len(snapshot["resources"]) > 500
                or not isinstance(snapshot.get("labels"), dict)):
            raise ValueError("Transform retained cleanup has no exact installed namespace and resource inventory")
        resources = [{"kind": "pods", "name": name, "uid": snapshot.get("uid")}, *snapshot["resources"]]
        for resource in resources:
            kind, resource_name, uid = (resource.get(key) for key in ("kind", "name", "uid"))
            if (not isinstance(uid, str) or not uid
                    or not (kind == "pods" and resource_name == name
                            or kind == "secrets" and resource_name == name
                            or kind == "configmaps" and isinstance(resource_name, str)
                            and re.fullmatch(re.escape(name) + r"-part-[0-9]{4}", resource_name))):
                raise ValueError("Transform cleanup inventory escaped its recorded owner")
            identity = (namespace, kind, resource_name)
            if identity in seen:
                raise ValueError("Transform cleanup resource appears in more than one receipt")
            seen.add(identity)
            await authority.revalidate()
            path = f"/api/v1/namespaces/{namespace}/{kind}/{resource_name}"
            value = await asyncio.to_thread(api.request, "GET", path, metadata_only=True,
                                          maximum=65536, timeout_seconds=2)
            authority.assert_current()
            if value is not None:
                raise ValueError("Transform recorded cleanup resource is still present; reconcile its original UID, do not resubmit")
            observed.append({"namespace": namespace, **resource, "present": False,
                             "checkpoint_sha256": digest(entry["checkpoint"])})
    return observed


async def bounded_get(client, path, maximum):
    async with client.stream("GET", path, follow_redirects=False) as response:
        response.raise_for_status()
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > maximum:
                raise ValueError("Transform retained response exceeds its bound")
        return bytes(content), response.headers


async def read_retained(client, observer, authority, run, result):
    """Re-read Product access and independent S3 bytes after owned resources are absent."""
    scope = authority.scope
    root = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
            f"{quote(scope['project_id'], safe='')}/services/transform")
    independent = {"original_patch": result["original_patch"]}
    for stage in ("before", "after"):
        for index, entry in enumerate(result["tests_" + stage]):
            for stream in ("stdout", "stderr"):
                independent[f"tests_{stage}-{index}-{stream}"] = {
                    **entry["receipt"][stream], "tenant_id": scope["tenant_id"], "project_id": scope["project_id"]}
    if set(result["retained_artifacts"]) != set(independent):
        raise ValueError("Transform retained patch/test inventory changed")
    records = {}
    for name, reference in result["retained_artifacts"].items():
        expiry = datetime.fromisoformat(reference["expires_at"].replace("Z", "+00:00"))
        if (reference.get("tenant_id") != scope["tenant_id"] or reference.get("project_id") != scope["project_id"]
                or expiry.tzinfo is None or expiry <= authority.clock() or reference.get("revoked_at")
                or type(reference.get("bytes")) is not int or not 0 <= reference["bytes"] <= 32 * 1024 * 1024):
            raise ValueError("Transform retained artifact authority or retention changed")
        path = f"{root}/artifacts/{quote(reference['artifact_id'], safe='')}"
        await authority.revalidate()
        access, _ = await bounded_get(client, path, 65536)
        access = json.loads(access)
        if access.get("artifact") != reference or access.get("download_path") != path + "/content":
            raise ValueError("Transform retained Product access differs from original result")
        content, _ = await bounded_get(client, path + "/content", reference["bytes"])
        stored = await observer.artifacts.read(reference=independent[name], authority=authority)
        authority.assert_current()
        if (len(content) != reference["bytes"] or sha256(content).hexdigest() != reference["sha256"]
                or content != stored):
            raise ValueError("Transform retained bytes differ from independent storage after cleanup")
        records[name] = {"reference": reference, "bytes_sha256": sha256(content).hexdigest(),
                         "independent_reference": independent[name]}
    await authority.revalidate()
    latest, headers = await bounded_get(client, root + "/results/" + quote(result["result_id"], safe=""), 4 * 1024 * 1024)
    if json.loads(latest) != result or headers.get("etag") != f'"{result["revision"]}"':
        raise ValueError("Transform selected result changed after cleanup readback")
    return records


class InstalledRetained:
    def __init__(self, evidence, native_results, installed, artifact_bucket):
        self.evidence, self.native_results, self.installed = evidence, native_results, installed
        self.artifact_bucket = artifact_bucket
        self.api = None
        self.completed = []

    async def collect(self, **values):
        self.stage = "selected-original-case"
        try:
            await self._collect(**values)
        except Exception as error:
            # Keep collection failures separate from native validation. Reading
            # the second original case remains useful; no work is resubmitted.
            receipt = self.evidence.journal.append("retained-collection-incomplete", {
                "stage": self.stage, "exception_type": type(error).__name__,
                "run_id": values["run"].run_id, "next_action": COLLECTION_ACTIONS[self.stage]})
            self.evidence.progress.append({"kind": self.stage, "outcome": "incomplete",
                "receipt_sha256": receipt, "next_action": COLLECTION_ACTIONS[self.stage]})

    async def _collect(self, *, run, attempt, result, authority, observer, intent, files, client, sessions):
        matching = [case for case in self.native_results.cases if case["run_id"] == run.run_id
                    and case["scope"] == authority.scope and case["result_sha256"] == digest(result)]
        if len(matching) != 1:
            raise ValueError("Transform retained readback is not an independently verified original case")
        case = matching[0]
        self.stage = "durable-progress"
        history = await read_history(sessions, run, attempt, result)
        history_receipt = self.evidence.journal.append("durable-progress-readback", history)
        self.evidence.progress.append({"kind": "recovery-and-reuse", "case_sha256": digest(case),
                                       "receipt_sha256": history_receipt, "missing": history["missing"]})
        # The source manifest and test policy are independently checked by the
        # native evaluator. They identify a project; they do not prove that its
        # repository is non-ADP or that no platform edits occurred between runs.
        portability = {"case": case, "sources": run.manifest["sources"],
                       "test_commands": run.admission["transform_test_policy"]["commands"],
                       "cost": {"status": "unknown", "amount_usd": None, "currency": None,
                                "reason": "no measured billing attribution in installed run records"},
                       "missing": ["independent-repository-identity", "deployment-history",
                                   "adopted-resource-and-scm-before-after"]}
        self.evidence.journal.append("portability-readback", portability)
        if self.installed.series is None:
            self.evidence.progress.append({"kind": "retention-after-cleanup", "case_sha256": digest(case),
                "missing": ["installed-cluster-selection"]})
            return
        self.stage = "installed-cleanup-target"
        if self.api is None:
            series = self.installed.series
            self.api, self.namespaces, target = await asyncio.to_thread(installed_inventory,
                series.config, series.fixture, series.context, self.artifact_bucket)
            self.evidence.journal.append("cleanup-target-readback", target)
        self.stage = "retained-checkpoints"
        checkpoints = await observer.verify_checks(intent=intent, run=run, result=result,
                                                  base_files=files, authority=authority)
        self.stage = "owned-resource-absence"
        absence = await read_absence(self.api, self.namespaces, checkpoints, authority)
        self.stage = "retention-after-cleanup"
        retained = await read_retained(client, observer, authority, run, result)
        observation = {"case": case, "absence": absence, "retained_artifacts": retained,
                       "observed_at": datetime.now(UTC).isoformat()}
        receipt = self.evidence.journal.append("retained-after-cleanup", observation)
        self.completed.append({"case": case, "receipt_sha256": receipt})
        self.evidence.progress.append({"kind": "owned-scratch-cleanup", "case_sha256": digest(case),
            "receipt_sha256": receipt, "absent_count": len(absence),
            "missing": ["adopted-resource-preservation", "independent-scm-before-after"]})

    def finish(self):
        if (len(self.completed) == 2 and len(self.native_results.cases) == 2
                and {digest(item["case"]) for item in self.completed}
                    == {digest(case) for case in self.native_results.cases}):
            self.evidence._collected("retained-results", {"cases": self.completed})

    def close(self):
        if self.api is not None:
            self.api.client.close()
