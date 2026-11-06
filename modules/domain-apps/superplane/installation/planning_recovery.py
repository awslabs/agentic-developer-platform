"""Installation-owner evidence for an expired initial planning start barrier.

Collects live protected EKS audit provenance, retained storage and probe results.
It accepts selectors, never a caller-supplied absence claim. Publishing evidence
does not recover an operation: Gateway independently checks the admission,
expired grants/sessions and a fresh human decision under its operation lock.
"""

import json
import hashlib
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .planning_probe import probe_job, verify_report
from .lifecycle_foundations import probe_matches
from .provider_authority import contract, installed_config, require


def kubectl(*args):
    return subprocess.check_output(
        ["kubectl", "--request-timeout=30s", *args], text=True, timeout=45
    )


def source_hashes(image):
    require(re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image))
    code = """import importlib.util,pathlib,hashlib,json
r=pathlib.Path(importlib.util.find_spec('workspace_provisioning').origin).parent
print(json.dumps({str(p.relative_to(r)):hashlib.sha256(p.read_bytes()).hexdigest()
 for p in r.rglob('*.py') if 'tests' not in p.parts and '_data' not in p.parts}))
"""
    return json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "/opt/executor/bin/python",
                image,
                "-B",
                "-c",
                code,
            ],
            text=True,
            timeout=120,
        )
    )


def source_pod(
    session,
    *,
    cluster,
    namespace,
    name,
    uid,
    acquired_at,
    now,
    management_region=None,
    historical_bootstrap=False,
    bootstrap_diagnostics=False,
    environment_history=None,
    planning_diagnostics=None,
    provenance_assumption=None,
):
    require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,252}", name))
    # Authenticated archived history or the exact approved diagnostic manifest
    # can identify an older bootstrap. Ordinary collection retains its 24h bound.
    require(type(historical_bootstrap) is bool and type(bootstrap_diagnostics) is bool)
    require(environment_history is None or bootstrap_diagnostics)
    if provenance_assumption is not None:
        from .bootstrap_diagnostic_assumption import ApprovedDiagnosticAssumption

        require(type(provenance_assumption) is ApprovedDiagnosticAssumption)
        require(
            bootstrap_diagnostics
            and environment_history is None
            and not historical_bootstrap
            and planning_diagnostics is None
        )
        provenance_assumption.select(cluster, namespace, name, uid)
    archived_assumption = (
        provenance_assumption is not None and acquired_at <= now - timedelta(days=1)
    )
    maximum_age = timedelta(
        days=30 if historical_bootstrap or archived_assumption else 1
    )
    require(now - maximum_age < acquired_at < now - timedelta(minutes=15))
    client = (
        session.client("logs", region_name=management_region)
        if management_region
        else session.client("logs")
    )
    group = f"/aws/eks/{cluster}/cluster"
    start = acquired_at - timedelta(minutes=5)
    if historical_bootstrap or archived_assumption:
        groups = [
            item
            for page in client.get_paginator("describe_log_groups").paginate(
                logGroupNamePrefix=group
            )
            for item in page.get("logGroups", [])
            if item.get("logGroupName") == group
        ]
        require(len(groups) == 1)
        retained = groups[0]
        require(
            type(retained.get("creationTime")) is int
            and retained["creationTime"] <= int(start.timestamp() * 1000)
        )
        days = retained.get("retentionInDays")
        require(
            days is None
            or (type(days) is int and days > 0 and timedelta(days=days) >= now - start)
        )
        # Retention eligibility is not an absence proof. Read the complete
        # original window through actual now and retain all audit checks below.
    pattern = json.dumps(name)
    if bootstrap_diagnostics:
        from .bootstrap_diagnostics import component_source

        if (
            component_source(cluster, namespace, name)
            or provenance_assumption is not None
        ):
            # Keep every mutation and interactive stream for this finite source.
            # Ordinary reads cannot alter provenance and exceed the event bound.
            # Generated-name creates omit objectRef.name: retain their response.
            pattern = (
                '{ $.objectRef.resource = "pods" && $.objectRef.namespace = '
                + json.dumps(namespace)
                + " && ($.objectRef.name = "
                + json.dumps(name)
                + " || $.responseObject.metadata.name = "
                + json.dumps(name)
                + ') && (($.verb != "get" && $.verb != "list" && $.verb != "watch")'
                + ' || $.objectRef.subresource = "exec"'
                + ' || $.objectRef.subresource = "attach"'
                + ' || $.objectRef.subresource = "ephemeralcontainers") }'
            )
    events = []
    for page in client.get_paginator("filter_log_events").paginate(
        logGroupName=group,
        logStreamNamePrefix="kube-apiserver-audit-",
        startTime=int(start.timestamp() * 1000),
        endTime=int(now.timestamp() * 1000),
        filterPattern=pattern,
    ):
        for event in page.get("events", []):
            if event["logStreamName"].startswith("kube-apiserver-audit-"):
                value = json.loads(event["message"])
                if value.get("objectRef", {}).get("namespace") == namespace:
                    events.append((event, value))
        require(len(events) <= 10000)
    creates, terminals = [], []
    for event, value in events:
        ref = value.get("objectRef", {})
        if ref.get("resource") != "pods":
            continue
        if ref.get("subresource") in {"exec", "attach", "ephemeralcontainers"}:
            require(bootstrap_diagnostics or planning_diagnostics is not None)
            continue
        pod = value.get("responseObject", {})
        if pod.get("metadata", {}).get("uid") != uid:
            continue
        require(
            pod["metadata"]["name"] == name
            and pod["metadata"]["namespace"] == namespace
        )
        if (
            value.get("stage") == "ResponseComplete"
            and value.get("verb") == "create"
            and not ref.get("subresource")
        ):
            require(value.get("responseStatus", {}).get("code") == 201)
            creates.append((event, pod))
        if pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}:
            terminals.append((event, pod))
    require(len(creates) == 1 and terminals)
    event, pod = creates[0]
    require(
        datetime.fromisoformat(
            pod["metadata"]["creationTimestamp"].replace("Z", "+00:00")
        )
        <= acquired_at
    )
    require(
        len(pod["spec"]["containers"]) == 1
        and not pod["spec"].get("initContainers")
        and not pod["spec"].get("ephemeralContainers")
    )
    container = pod["spec"]["containers"][0]
    require(not container.get("command") and not container.get("args"))
    require(
        all(x["spec"]["containers"] == pod["spec"]["containers"] for _, x in terminals)
    )
    audit = {
        "log_group": group,
        "creation_event_id": event["eventId"],
        "terminal_event_ids": sorted(e["eventId"] for e, _ in terminals),
    }
    if provenance_assumption is not None:
        audit["provenance_assumption"] = provenance_assumption.qualify(
            events,
            cluster=cluster,
            pod=pod,
            creation_event=event,
            terminals=terminals,
            session=session,
            management_region=management_region,
        )
    elif bootstrap_diagnostics:
        from .bootstrap_diagnostics import qualify

        historical = qualify(
            events,
            cluster=cluster,
            pod=pod,
            creation_event=event,
            terminals=terminals,
            session=session,
            management_region=management_region,
            environment_history=environment_history,
            audit=audit,
        )
        if historical is not None:
            audit["historical_environment"] = historical
    if planning_diagnostics is not None:
        from .planning_diagnostics import qualify

        audit["planning_diagnostics"] = qualify(
            events,
            cluster=cluster,
            pod=pod,
            terminals=terminals,
            session=session,
            management_region=management_region,
            operator=planning_diagnostics,
        )
    return pod, audit


def collect(
    enrollment,
    original,
    descriptor,
    probe_name,
    session,
    *,
    read_kube=kubectl,
    image_sources=source_hashes,
    source_root=None,
    completion=False,
    maintenance_receipt=None,
    partial_apply=False,
    interrupted=False,
    policy_extension=None,
):
    require(sum((completion, partial_apply, interrupted)) <= 1)
    require(policy_extension is None or (interrupted and maintenance_receipt is None))
    schema = contract()
    value = schema.validate(enrollment["authority"])
    region, account = value["region"], value["account_id"]
    management_region = enrollment.get("management_region", region)
    caller = session.client("sts", region_name=region).get_caller_identity()
    owner = enrollment["operator_role_arn"]
    require(
        caller["Account"] == account
        and caller["UserId"].split(":", 1)[0] == enrollment["operator_role_id"]
    )
    require(
        caller["Arn"].startswith(
            f"arn:aws:sts::{account}:assumed-role/{owner.rsplit('/', 1)[1]}/"
        )
    )
    role = session.client("iam").get_role(RoleName=owner.rsplit("/", 1)[1])["Role"]
    require((role["Arn"], role["RoleId"]) == (owner, enrollment["operator_role_id"]))
    require(
        original["installation_id"] == value["installation_id"]
        and original["status"] == "installed-and-verified"
    )
    cluster = session.client("eks", region_name=management_region).describe_cluster(
        name=enrollment["management_cluster"]
    )["cluster"]
    configured = installed_config(enrollment["gateway_namespace"], cluster)
    table = f"adp-{enrollment['environment']}-superplane-provider-authorities"
    require(configured["ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE"] == table)
    stored = (
        session.client("dynamodb", region_name=management_region)
        .get_item(
            TableName=table,
            Key={"record_id": {"S": value["credential_id"]}},
            ConsistentRead=True,
        )
        .get("Item")
    )
    require(
        stored is not None
        and stored["document"]["S"] == schema.canonical(value)
        and stored["revision"]["S"] == schema.digest(value)
    )
    namespace = original["environment"]["namespace"]
    bindings = [
        b
        for b in json.loads(configured["ADP_DOMAIN_OPERATION_BINDINGS"])
        if b.get("domain") == "superplane" and b.get("org_id") == value["org_id"]
    ]
    require(
        len(bindings) == 1
        and bindings[0]["worker_namespace"] == namespace
        and bindings[0]["adp_org_id"] == value["adp_org_id"]
    )

    def obj(kind, name):
        return json.loads(read_kube("-n", namespace, "get", kind, name, "-o", "json"))

    scaled = obj("scaledjob", "superplane-paid-worker")
    require(scaled["metadata"]["annotations"]["autoscaling.keda.sh/paused"] == "true")
    api = obj("deployment", "superplane-api")
    api_container = next(
        c
        for c in api["spec"]["template"]["spec"]["containers"]
        if c["name"] == "superplane-api"
    )
    require(
        next(
            e["value"]
            for e in api_container["env"]
            if e["name"] == "SUPERPLANE_OPERATION_DISPATCH_ENABLED"
        )
        == "false"
    )
    claim_name = original["environment"]["paid_worker"]["lifecycle_state_claim"]
    pvc = obj("pvc", claim_name)
    pv = obj("pv", pvc["spec"]["volumeName"])
    require(
        pvc["metadata"]["uid"]
        == original["lifecycle_foundations"]["PersistentVolumeClaim"]["uid"]
    )
    require(
        pv["metadata"]["uid"]
        == original["lifecycle_foundations"]["PersistentVolume"]["uid"]
    )
    require(
        pv["spec"]["claimRef"]["uid"] == pvc["metadata"]["uid"]
        and pvc["status"]["phase"] == pv["status"]["phase"] == "Bound"
    )
    pods = json.loads(read_kube("-n", namespace, "get", "pods", "-o", "json"))["items"]
    require(
        not any(
            p["status"]["phase"] not in {"Succeeded", "Failed"}
            and any(
                v.get("persistentVolumeClaim", {}).get("claimName") == claim_name
                for v in p["spec"].get("volumes", [])
            )
            for p in pods
        )
    )
    identity = [descriptor[k] for k in ("org_id", "workspace_id", "operation_id")]
    require(identity[:2] == [value["org_id"], value["workspace_id"]])
    job = obj("job", probe_name)
    template = json.loads(
        json.dumps(scaled["spec"]["jobTargetRef"]["template"]["spec"])
    )
    if descriptor.get("source_image"):
        template["containers"][0]["image"] = descriptor["source_image"]
    expected = probe_job(
        template,
        namespace=namespace,
        claim_name=claim_name,
        identity=identity,
        run_id=probe_name.rsplit("-", 1)[1],
        completion=completion,
        partial_source=descriptor["original_planning_operation_id"]
        if partial_apply
        else None,
    )
    spec = job["spec"]["template"]["spec"]
    require(probe_matches(spec, expected["spec"]["template"]["spec"]))
    require(
        any(
            c.get("type") == "Complete" and c.get("status") == "True"
            for c in job.get("status", {}).get("conditions", [])
        )
    )
    report = json.loads(read_kube("-n", namespace, "logs", "job/" + probe_name).strip())
    root = (
        Path(source_root)
        if source_root is not None
        else Path(__file__).resolve().parents[1] / "workspace_provisioning"
    )
    verify_report(report, identity, root)
    boundary = None
    if partial_apply:
        from workspace_provisioning.saved_plan_validation import (
            COMPLETION_RETRY_RUNTIME,
        )

        require(report["operation_directory_exists"] and report.get("partial_apply"))
        require(not descriptor.get("attempt_extension"))
        require(
            {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in COMPLETION_RETRY_RUNTIME
            }
            == COMPLETION_RETRY_RUNTIME
        )
        observation = report["partial_apply"]
        require(
            observation["source_operation_id"]
            == descriptor["original_planning_operation_id"]
        )
        require(observation["source_files"] == descriptor["original_artifact_files"])
        require(observation["module_sha256"] == descriptor["original_module_sha256"])
        boundary = {"kind": "partial-apply-state-v1", "observation": observation}
    elif interrupted:
        from workspace_provisioning.interrupted_plan import verify_source

        require(report["operation_directory_exists"])
        require(not descriptor.get("attempt_extension"))
        boundary = {
            "kind": "interrupted-read-only-plan-v1",
            "read_only_recipe": True,
            "qualified_source": verify_source(root),
            "entries": report["retained_entries"],
        }
    elif completion:
        from workspace_provisioning.saved_plan_validation import (
            SAVED_PLAN_RUNTIME,
            COMPLETION_RETRY_RUNTIME,
        )
        from workspace_provisioning.pricing_plan_validation import PRICING_RUNTIME

        accepted_runtimes = (
            (PRICING_RUNTIME,)
            if report.get("saved_plan", {}).get("pricing_revalidation")
            else (SAVED_PLAN_RUNTIME, PRICING_RUNTIME, COMPLETION_RETRY_RUNTIME)
        )
        require(report["operation_directory_exists"] and report.get("saved_plan"))
        require(not descriptor.get("attempt_extension"))
        runtime_pins = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in SAVED_PLAN_RUNTIME
        }
        require(runtime_pins in accepted_runtimes)
        boundary = {
            "kind": "saved-plan-finalization-v1",
            "entries": report["retained_entries"],
            "observation": report["saved_plan"],
        }
    elif report["operation_directory_exists"]:
        from workspace_provisioning.planning_boundary import verify_pre_init

        entries = report.get("retained_entries")
        require(isinstance(entries, dict))
        source = root.parent / "infra" / "workspaces"
        if "module/.terraform/terraform.tfstate" not in entries:
            boundary = {
                "kind": "before-terraform-init-v1",
                "entries": entries,
                "sha256": verify_pre_init(entries, source),
            }
        else:
            # Complete source/pod/volume verification below before accepting this
            # narrower initialized boundary. Never relabel it as pre-init.
            boundary = {
                "kind": (
                    "provider-lock-refusal-v1"
                    if report.get("provider_lock_refusal") is not None
                    else "provider-socket-refusal-v1"
                    if report.get("provider_socket_refusal") is not None
                    else "incomplete-provider-install-v1"
                ),
                "entries": entries,
            }
    extension = descriptor.get("attempt_extension")
    if extension is not None:
        require(
            isinstance(extension, dict)
            and set(extension)
            == {
                "authorized_by",
                "authorization",
                "previous_limit",
                "recorded_attempts",
                "additional_attempts",
            }
        )
        require(extension["authorized_by"] == value["subject"])
        require(
            isinstance(extension["authorization"], str)
            and 1 <= len(extension["authorization"].strip()) <= 2048
        )
        require(
            all(
                type(extension[k]) is int
                for k in ("previous_limit", "recorded_attempts", "additional_attempts")
            )
        )
        require(
            1 <= extension["additional_attempts"] <= 5
            and 1 <= extension["recorded_attempts"] <= extension["previous_limit"]
        )
        require(extension["previous_limit"] + extension["additional_attempts"] <= 10)
    now = datetime.now(timezone.utc)
    acquired = datetime.fromisoformat(descriptor["source_acquired_at"])
    pod, provenance = source_pod(
        session,
        cluster=enrollment["management_cluster"],
        namespace=namespace,
        name=descriptor["source_pod_name"],
        uid=descriptor["workload_binding"],
        acquired_at=acquired,
        now=now,
        **(
            {
                "planning_diagnostics": {
                    "arn": owner,
                    "role_id": enrollment["operator_role_id"],
                }
            }
            if interrupted
            else {}
        ),
        **(
            {"management_region": management_region}
            if management_region != region
            else {}
        ),
    )
    container = pod["spec"]["containers"][0]
    mount = next(m for m in container["volumeMounts"] if m["mountPath"] == "/run/state")
    require(container.get("securityContext", {}).get("runAsUser") == 65531)
    require(container.get("securityContext", {}).get("runAsGroup") == 65532)
    require(not mount.get("subPath") and not mount.get("subPathExpr"))
    volume = next(v for v in pod["spec"]["volumes"] if v["name"] == mount["name"])
    require(volume["persistentVolumeClaim"]["claimName"] == claim_name)
    require(
        [
            e.get("value")
            for e in container["env"]
            if e["name"] == "SUPERPLANE_LIFECYCLE_STATE_DIR"
        ]
        == ["/run/state"]
    )
    require(
        not descriptor.get("source_image")
        or descriptor["source_image"] == container["image"]
    )
    if source_root is not None:
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        image_revision = subprocess.check_output(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                container["image"],
            ],
            text=True,
        ).strip()
        require(revision == image_revision)
    source = image_sources(container["image"])
    require(
        source
        == {
            k: v
            for k, v in report["runtime_sources"].items()
            if not k.startswith("_data/")
        }
    )
    if boundary and boundary["kind"] in {
        "incomplete-provider-install-v1",
        "provider-lock-refusal-v1",
        "provider-socket-refusal-v1",
        "saved-plan-finalization-v1",
        "partial-apply-state-v1",
        "interrupted-read-only-plan-v1",
    }:
        from workspace_provisioning.planning_boundary import (
            verify_incomplete_init,
            verify_provider_lock_refusal,
        )

        settings = original["environment"]["paid_worker"]
        extension_uid = None
        if policy_extension is not None:
            from .planning_policy_receipt import extension_settings

            settings, extension_uid = extension_settings(
                policy_extension, original, container["image"], value["workspace_id"]
            )
        if maintenance_receipt is not None:
            require(
                maintenance_receipt.get("status") == "maintenance-complete"
                and not maintenance_receipt.get("remote_lock")
                and not maintenance_receipt.get("lock_attempt")
                and maintenance_receipt.get("installation_id")
                == original["installation_id"]
                and maintenance_receipt.get("release_id")
                == schema.digest(maintenance_receipt.get("release_lock"))
                and maintenance_receipt.get("release_lock", {})
                .get("images", {})
                .get("superplane-paid-worker")
                == container["image"].rsplit("@", 1)[-1]
                and all(
                    maintenance_receipt.get("environment", {}).get(k)
                    == original["environment"][k]
                    for k in (
                        "account_id",
                        "region",
                        "cluster",
                        "namespace",
                        "org_id",
                        "adp_org_id",
                    )
                )
                and maintenance_receipt.get("maintenance", {}).get("worker")
                == container["image"]
            )
            settings = maintenance_receipt["environment"]["paid_worker"]
            require(
                maintenance_receipt["maintenance"].get("regional_policy")
                == settings["lifecycle_policy_configmap"]
            )
        policy = obj("configmap", settings["lifecycle_policy_configmap"])
        raw = policy["data"]["lifecycle.json"]
        require(
            policy.get("immutable") is True
            and hashlib.sha256(raw.encode()).hexdigest()
            == settings["lifecycle_policy_sha256"]
            and (
                policy["metadata"]["uid"]
                == original["lifecycle_foundations"]["ConfigMap"]["uid"]
                if maintenance_receipt is None and policy_extension is None
                else policy["metadata"]["creationTimestamp"]
                <= pod["metadata"]["creationTimestamp"]
            )
            and (extension_uid is None or policy["metadata"]["uid"] == extension_uid)
        )
        policy_mount = next(
            m for m in container["volumeMounts"] if m["mountPath"] == "/run/policy"
        )
        policy_volume = next(
            v for v in pod["spec"]["volumes"] if v["name"] == policy_mount["name"]
        )
        require(
            policy_mount.get("readOnly") is True
            and not policy_mount.get("subPath")
            and not policy_mount.get("subPathExpr")
            and policy_volume["configMap"]["name"]
            == settings["lifecycle_policy_configmap"]
            and [
                e.get("value")
                for e in container["env"]
                if e["name"] == "SUPERPLANE_LIFECYCLE_POLICY_FILE"
            ]
            == ["/run/policy/lifecycle.json"]
        )
        from workspace_provisioning.lifecycle_policy import select_policy

        document = json.loads(raw)
        config = (
            select_policy(document, value["org_id"], value["workspace_id"]).runtime
            if "workspace_policies" in document
            else document["tenants"][value["org_id"]]["runtime"]
        )
        target = {
            "account_id": account,
            "aws_region": region,
            "environment": config["environment"],
            "org_id": value["org_id"],
            "workspace_id": value["workspace_id"],
        }
        verifier = verify_incomplete_init
        extra = {}
        if partial_apply:
            from workspace_provisioning.partial_apply_backend import read_snapshot

            require(maintenance_receipt is not None)
            observation = boundary["observation"]
            require(observation["apply_started"] is True)
            boundary["sha256"] = schema.digest(observation)
            boundary["backend_observation"] = read_snapshot(
                session, config["backend"], target
            )
            cluster = session.client("eks", region_name=region).describe_cluster(
                name=boundary["backend_observation"]["resources"][
                    "aws_eks_cluster.workspace"
                ]["id"]
            )["cluster"]
            require(
                cluster["arn"] == boundary["backend_observation"]["cluster_arn"]
                and cluster["status"] == "ACTIVE"
            )
            require(
                not session.client("eks", region_name=region)
                .list_nodegroups(clusterName=cluster["name"])
                .get("nodegroups")
            )
            boundary["cluster_status"] = cluster["status"]
        elif completion:
            from workspace_provisioning.saved_plan_validation import (
                verify_saved_entries,
            )

            observation = boundary["observation"]
            pricing = observation.get("pricing_revalidation")
            if pricing:
                from workspace_provisioning.pricing_plan_validation import (
                    verify_pricing_entries,
                )

                verifier = verify_pricing_entries
            else:
                verifier = verify_saved_entries
            require(
                observation.get("offline_guard_passed") is True
                and observation.get("initial_create_only") is True
            )
            require(all(observation["target"].get(k) == v for k, v in target.items()))
            require(
                all(
                    boundary["entries"].get("review/" + name)
                    == {"kind": "file", "sha256": digest}
                    for name, digest in (
                        pricing["original_files"] if pricing else observation["files"]
                    ).items()
                )
            )
        elif interrupted:
            from workspace_provisioning.interrupted_plan import verify_retained

            verifier = verify_retained
        elif boundary["kind"] == "provider-lock-refusal-v1":
            verifier = verify_provider_lock_refusal
            boundary["provider_lock_refusal"] = report["provider_lock_refusal"]
            extra["observation"] = report["provider_lock_refusal"]
        elif boundary["kind"] == "provider-socket-refusal-v1":
            from workspace_provisioning.provider_socket_boundary import (
                verify_provider_socket_refusal,
            )

            verifier = verify_provider_socket_refusal
            boundary["provider_socket_refusal"] = report["provider_socket_refusal"]
            extra = {
                "observation": report["provider_socket_refusal"],
                "runtime_source": root,
                "operation_directory_name": hashlib.sha256(
                    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        if not partial_apply:
            boundary["sha256"] = verifier(
                boundary["entries"],
                root.parent / "infra" / "workspaces",
                report.get("initialization_documents"),
                target=target,
                backend=config["backend"],
                **extra,
            )
            if interrupted:
                from .interrupted_plan_backend import observe

                live_pod = obj("pod", descriptor["source_pod_name"])
                require(live_pod["metadata"]["uid"] == descriptor["workload_binding"])
                statuses = live_pod["status"]["containerStatuses"]
                require(len(statuses) == 1 and statuses[0]["state"].get("terminated"))
                stopped = datetime.fromisoformat(
                    statuses[0]["state"]["terminated"]["finishedAt"].replace(
                        "Z", "+00:00"
                    )
                )
                boundary["backend_observation"] = observe(
                    session,
                    config["backend"],
                    target,
                    pod_name=descriptor["source_pod_name"],
                    acquired_at=acquired,
                    stopped_at=stopped,
                )
            else:
                boundary["backend_observation"] = empty_initial_backend(
                    session, config["backend"], target
                )
        boundary["lifecycle_policy_sha256"] = settings["lifecycle_policy_sha256"]
    # External audit/image reads take time. Refuse drift before returning a fact
    # that can be published under the installation owner's exclusive lock.
    latest = obj("scaledjob", "superplane-paid-worker")
    require(
        latest["spec"] == scaled["spec"]
        and latest["metadata"]["annotations"]["autoscaling.keda.sh/paused"] == "true"
    )
    require(obj("pvc", claim_name)["metadata"]["uid"] == pvc["metadata"]["uid"])
    require(
        obj("pv", pvc["spec"]["volumeName"])["metadata"]["uid"] == pv["metadata"]["uid"]
    )
    key = {
        k: descriptor[k]
        for k in (
            "org_id",
            "workspace_id",
            "operation_id",
            "plan_digest",
            "source_key",
            "source_fence",
        )
    }
    return {
        **key,
        "version": 1,
        "record_id": ("sppa1:" if partial_apply else "sppr1:") + schema.digest(key),
        "evidence_kind": "partial-apply-state-v1"
        if partial_apply
        else "interrupted-read-only-plan-v1"
        if interrupted
        else "saved-plan-finalization-v1"
        if completion
        else "planning-start-barrier-v1",
        "planning_started": completion or partial_apply or interrupted,
        **(
            {
                "apply_started": True,
                "source_version": descriptor["source_version"],
                "original_artifact_id": descriptor["original_artifact_id"],
                "original_planning_operation_id": descriptor[
                    "original_planning_operation_id"
                ],
                "snapshot": boundary["backend_observation"],
            }
            if partial_apply
            else {}
        ),
        **({"apply_started": False} if completion or interrupted else {}),
        **({"preparation_boundary": boundary} if boundary else {}),
        **({"attempt_extension": extension} if extension else {}),
        "credential_id": value["credential_id"],
        "installation_id": value["installation_id"],
        "adp_org_id": value["adp_org_id"],
        "account_id": account,
        "region": region,
        "source_principal": descriptor["source_principal"],
        "workload_binding": descriptor["workload_binding"],
        "source_pod_name": descriptor["source_pod_name"],
        "source_acquired_at": descriptor["source_acquired_at"],
        "source_image": container["image"],
        "source_hashes": source,
        "source_audit": provenance,
        "storage_uid": pvc["metadata"]["uid"],
        "volume_uid": pv["metadata"]["uid"],
        "probe_job_uid": job["metadata"]["uid"],
        "inspection_sha256": schema.digest(report),
        "checked_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }


def empty_initial_backend(session, backend, target):
    """Read exact versioned backend and lock identities; errors never mean absent.

    This is corroboration for the source/start barrier, not a claim that an
    arbitrary AWS listing can establish absence of infrastructure resources.
    """
    bucket, region = backend["bucket"], backend["region"]
    account = target["account_id"]
    key = (
        f"{target['environment']}/modules/superplane-workspaces/v2/"
        f"{target['org_id']}/{target['workspace_id']}/terraform.tfstate"
    )
    s3 = session.client("s3", region_name=region)
    require(
        s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=account).get(
            "Status"
        )
        == "Enabled"
    )
    versions = s3.list_object_versions(
        Bucket=bucket, Prefix=key, ExpectedBucketOwner=account, MaxKeys=1
    )
    require(
        not versions.get("Versions")
        and not versions.get("DeleteMarkers")
        and not versions.get("IsTruncated")
    )
    ddb = session.client("dynamodb", region_name=region)
    table = backend["lock_table"]
    require(
        ddb.describe_table(TableName=table)["Table"]["TableArn"]
        == (f"arn:aws:dynamodb:{region}:{account}:table/{table}")
    )
    for suffix in ("", "-md5"):
        require(
            not ddb.get_item(
                TableName=table,
                Key={"LockID": {"S": f"{bucket}/{key}{suffix}"}},
                ConsistentRead=True,
            ).get("Item")
        )
    return {
        "bucket": bucket,
        "key": key,
        "region": region,
        "lock_table": table,
        "versioning": "Enabled",
        "versions_absent": True,
        "delete_markers_absent": True,
        "lock_and_checksum_absent": True,
    }


def _publish(enrollment, evidence, session):
    """Call only with collect()'s fresh result while holding the installer lock."""
    schema = contract()
    client = session.client(
        "dynamodb",
        region_name=enrollment.get(
            "management_region", enrollment["authority"]["region"]
        ),
    )
    table = f"adp-{enrollment['environment']}-superplane-provider-authorities"
    item = {
        "record_id": {"S": evidence["record_id"]},
        "document": {"S": schema.canonical(evidence)},
        "revision": {"S": schema.digest(evidence)},
    }
    client.put_item(
        TableName=table,
        Item=item,
        ConditionExpression="attribute_not_exists(record_id)",
    )
    require(
        client.get_item(
            TableName=table, Key={"record_id": item["record_id"]}, ConsistentRead=True
        ).get("Item")
        == item
    )
    return {
        "state": "planning-evidence-recorded",
        "record_id": evidence["record_id"],
        "human_admitted": False,
    }


def record(
    installer,
    enrollment,
    original,
    descriptor,
    probe_name,
    session,
    *,
    check_only=True,
    source_root=None,
    completion=False,
    maintenance_receipt=None,
    partial_apply=False,
    fresh_inspection=False,
    interrupted=False,
    policy_extension=None,
):
    require(not fresh_inspection or partial_apply)
    require(
        installer.receipt.get("remote_lock")
        and installer.owner == enrollment["authority"]["installation_id"]
    )
    evidence = collect(
        enrollment,
        original,
        descriptor,
        probe_name,
        session,
        source_root=source_root,
        completion=completion,
        maintenance_receipt=maintenance_receipt,
        partial_apply=partial_apply,
        interrupted=interrupted,
        policy_extension=policy_extension,
    )
    if fresh_inspection:
        # A complete new collector run supplies a new one-hour proof. The prior
        # immutable proof and any admission bound to it remain untouched.
        require(re.fullmatch(r"[a-f0-9]{16}", installer.run_id))
        evidence.update(version=2, inspection_id=installer.run_id)
        identity = {
            k: evidence[k]
            for k in (
                "org_id",
                "workspace_id",
                "operation_id",
                "plan_digest",
                "source_key",
                "source_fence",
                "inspection_id",
            )
        }
        evidence["record_id"] = "sppa1:" + contract().digest(identity)
    if check_only:
        return {
            "state": "planning-evidence-reviewed",
            "human_admitted": False,
            "evidence": evidence,
        }
    result = _publish(enrollment, evidence, session)
    installer.receipt["planning_recovery_evidence"] = evidence
    installer.save()
    return result
