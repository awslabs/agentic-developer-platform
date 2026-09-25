"""Read original captured workload identities; never replay or adopt a POST."""

import hashlib
from types import SimpleNamespace

from harness_jobs.execution_descriptors import parse_execution_steps
from harness_jobs.execution_plan import step_key
from harness_jobs.identity import OperationRefused

from .workload_observation import _resources_equal


def selected_call(operation, plan, call):
    lease = operation.grant.lease
    record = SimpleNamespace(
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        operation_id=lease.operation_id,
        plan_digest=operation.plan_digest,
    )
    selected = next(
        (
            step
            for step in parse_execution_steps(
                operation.request.parameters["execution_steps"]
            )
            if step_key(record, step) == call["idempotency_key"]
        ),
        None,
    )
    if (
        selected is None
        or any(
            call[key] != value
            for key, value in {
                "operation_id": lease.operation_id,
                "org_id": lease.org_id,
                "workspace_id": lease.workspace_id,
                "job_id": operation.job_id,
                "allocation_id": operation.request.parameters["allocation_id"],
                "provider": selected.provider,
                "operation_kind": selected.operation_kind,
                "target": selected.target,
            }.items()
        )
        or selected.target != plan.cluster_name
    ):
        raise OperationRefused("original recovery provider descriptor differs")
    return selected


def contains(actual, expected):
    """Preserve approved values while allowing server-populated default fields."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual
            and (
                _resources_equal(actual[key], value)
                if key == "resources"
                else contains(actual[key], value)
            )
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(contains(a, b) for a, b in zip(actual, expected, strict=True))
        )
    return actual == expected


def approved_object(actual, expected, operation):
    """Exact effectful spec, with only enumerated Kubernetes defaults removed."""
    from copy import deepcopy

    if actual.get("metadata", {}).get("deletionTimestamp") is not None or not contains(
        actual, {"kind": expected["kind"], "metadata": expected["metadata"]}
    ):
        return False
    observed, approved = deepcopy(actual.get("spec", {})), expected["spec"]

    def defaults(value, submitted, fields):
        for key, default in fields.items():
            if key not in submitted and key in value:
                if value[key] != default:
                    return False
                value.pop(key)
        return True

    kind = expected["kind"]
    if kind == "Service":
        for key in ("clusterIP", "clusterIPs"):
            if key not in approved:
                observed.pop(key, None)
        if not defaults(
            observed,
            approved,
            {
                "ipFamilies": ["IPv4"],
                "ipFamilyPolicy": "SingleStack",
                "internalTrafficPolicy": "Cluster",
                "sessionAffinity": "None",
            },
        ):
            return False
        for port in observed.get("ports", []):
            if port.get("protocol", "TCP") != "TCP":
                return False
            port.pop("protocol", None)
    else:
        root_defaults = (
            {
                "parallelism": 1,
                "completions": 1,
                "completionMode": "NonIndexed",
                "suspend": False,
                "manualSelector": False,
                "podReplacementPolicy": "TerminatingOrFailed",
            }
            if kind == "Job"
            else {
                "revisionHistoryLimit": 10,
                "progressDeadlineSeconds": 600,
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"},
                },
            }
        )
        if not defaults(observed, approved, root_defaults):
            return False
        if kind == "Job" and "selector" not in approved and "selector" in observed:
            selector = observed.pop("selector")
            if selector not in (
                {
                    "matchLabels": {
                        "batch.kubernetes.io/controller-uid": actual["metadata"]["uid"]
                    }
                },
                {"matchLabels": {"controller-uid": actual["metadata"]["uid"]}},
            ):
                return False
        template = observed.get("template", {})
        template_meta = template.get("metadata", {})
        template_meta.pop("creationTimestamp", None)
        if kind == "Job":
            labels = template_meta.get("labels", {})
            for key, value in {
                "batch.kubernetes.io/controller-uid": actual["metadata"]["uid"],
                "controller-uid": actual["metadata"]["uid"],
                "batch.kubernetes.io/job-name": expected["metadata"]["name"],
                "job-name": expected["metadata"]["name"],
            }.items():
                if key in labels and labels.pop(key) != value:
                    return False
        pod, submitted = template.get("spec", {}), approved["template"]["spec"]
        if not defaults(
            pod,
            submitted,
            {
                "dnsPolicy": "ClusterFirst",
                "schedulerName": "default-scheduler",
                "serviceAccountName": "default",
                "serviceAccount": "default",
                "enableServiceLinks": True,
                "terminationGracePeriodSeconds": 30,
            },
        ):
            return False
        if len(pod.get("containers", [])) != len(submitted["containers"]):
            return False
        for container, original in zip(
            pod["containers"], submitted["containers"], strict=True
        ):
            if not defaults(
                container,
                original,
                {
                    "imagePullPolicy": "IfNotPresent",
                    "terminationMessagePath": "/dev/termination-log",
                    "terminationMessagePolicy": "File",
                    "stdin": False,
                    "stdinOnce": False,
                    "tty": False,
                },
            ):
                return False
            for port in container.get("ports", []):
                if port.get("protocol", "TCP") != "TCP":
                    return False
                port.pop("protocol", None)
            if not _resources_equal(
                container.get("resources", {}), original["resources"]
            ):
                return False
            container["resources"] = original["resources"]
    return observed == approved


async def observe(provider, operation, target, plan, call, authorize):
    selected = selected_call(operation, plan, call)
    if (
        operation.request.action != "provision"
        or "controller_deployment_id" not in operation.request.parameters
    ):
        return "unknown", None
    provider.workspace.require_dedicated_node_authority(target)
    await authorize()
    readiness = "3" if plan.node_bootstrap is not None else "2"
    if selected.operation_kind == "status" and selected.step_id == readiness:
        instances = await provider.instances(operation, plan)
        ready = await provider.workspace.ready_nodes(operation, target, plan, instances)
        await authorize()
        return ("succeeded", None) if ready else ("unknown", None)

    lease = operation.grant.lease
    async with provider.execution_pool.acquire() as connection:
        creating = await connection.fetchrow(
            "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1 AND org_id=$2 "
            "AND workspace_id=$3 AND allocation_id=$4 AND provider='aws' AND operation_kind='deploy'",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            operation.request.parameters["allocation_id"],
        )
        if creating is None:
            return "unknown", None
        selected_call(operation, plan, creating)
        rows = await connection.fetch(
            "SELECT provider_reference FROM harness_allocation_resource WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3 AND allocation_id=$4 AND provider='aws' "
            "AND kind='workspace_object' AND attempt_id=$5 AND fence_token=$6 "
            "AND $7=ANY(operation_keys)",
            lease.operation_id,
            lease.org_id,
            lease.workspace_id,
            operation.request.parameters["allocation_id"],
            creating["attempt_id"],
            creating["fence_token"],
            creating["idempotency_key"],
        )
    known = frozenset(row["provider_reference"] for row in rows)
    objects = provider.workspace.objects(operation, target, plan)
    from .workload_submissions import originals as submitted_objects

    submitted = await submitted_objects(provider, operation, creating)
    identities = [
        (obj["kind"], obj["metadata"]["namespace"], obj["metadata"]["name"])
        for obj in objects
    ]
    if submitted is None or set(submitted) != set(identities):
        return "unknown", None
    # Use the immutable pre-POST body, including its original absolute-duration
    # choice; never recompute the submitted deadline from a recovery lease.
    objects = [submitted[identity] for identity in identities]
    originals = []
    for obj in objects:
        prefix = (
            f"kubernetes:{obj['kind']}:{target['namespace']}:{obj['metadata']['name']}:"
        )
        matches = [ref for ref in known if ref.startswith(prefix)]
        if len(matches) != 1:
            # Even a perfect metadata/nonce match cannot replace a lost server UID.
            return "unknown", None
        originals.append(matches[0])
    if len(known) != len(originals):
        return "unknown", None
    observations = []
    for obj, reference in zip(objects, originals, strict=True):
        await authorize()
        response = await provider.workspace.request(
            operation,
            target,
            "GET",
            provider.workspace.path(target, obj["kind"], obj["metadata"]["name"]),
        )
        await authorize()
        if response.status_code == 404:
            observations.append(None)
            continue
        if response.status_code != 200:
            return "unknown", None
        actual = response.json()
        if provider.workspace.reference(
            obj["kind"], actual
        ) != reference or not approved_object(actual, obj, operation):
            return "unknown", None
        observations.append(actual)
    if selected.operation_kind == "deploy":
        # Root 404 does not prove dependent-Pod absence, nor permit another POST.
        # Only allocation inventory can assess cleanup of the captured identities.
        if all(obj is not None for obj in observations):
            return "succeeded", originals[0]
        return "unknown", None
    if any(obj is None for obj in observations):
        return "unknown", None
    if plan.node_bootstrap is not None:
        from .node_command_inventory import require_completed

        await require_completed(provider, operation, plan, authorize)
    pod_uids = set()

    async def placement(pod):
        proof = await provider.verify_pod_allocation(
            operation, target, plan, pod, authorize
        )
        pod_uids.add(pod["metadata"]["uid"])
        return proof

    if not await provider.workspace.workload_ready(
        operation,
        target,
        plan,
        known_references=known,
        authorize=authorize,
        verify_placement=placement,
    ):
        return "unknown", None
    if plan.data["workload"]["kind"] == "batch":
        # Recovery reads retained output; it never publishes a missing result under
        # an expired execution grant or turns a Job counter into a CUDA receipt.
        async with provider.domain_pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM controller_batch_results WHERE operation_id=$1 AND org_id=$2::text::uuid "
                "AND workspace_id=$3::text::uuid AND allocation_id=$4 AND plan_digest=$5 "
                "AND deployment_id=$6::text::uuid AND job_uid=$7",
                lease.operation_id,
                lease.org_id,
                lease.workspace_id,
                operation.request.parameters["allocation_id"],
                operation.plan_digest,
                operation.request.parameters["controller_deployment_id"],
                observations[0]["metadata"]["uid"],
            )
        if (
            row is None
            or row["pod_uid"] not in pod_uids
            or hashlib.sha256(row["content"].encode()).hexdigest() != row["sha256"]
        ):
            return "unknown", None
    await authorize()
    return "succeeded", None
