"""Installation-owner qualification of an original stopped bootstrap image.

This records source and pod provenance only. Gateway independently establishes
expired authority and absence of protected mutation journals under the operation
lock. No directory inspection or caller-supplied cloud absence authorizes retry.
"""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

from .planning_recovery import kubectl, source_pod
from .provider_authority import contract, installed_config, require


def source_contract():
    path = (
        Path(__file__).resolve().parents[4]
        / "modules/gateway/src/shared/domain_bootstrap_boundary.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_boundary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image_sources(image, names):
    code = """import hashlib,importlib.util,json,pathlib
names=json.loads(input())
roots={n:pathlib.Path(importlib.util.find_spec(n).origin).parent for n in ('workspace_provisioning','superplane_bootstrap')}
result={}
for name in names:
 package,relative=(('superplane_bootstrap',name.split('superplane_bootstrap/',1)[1]) if name.startswith('workspace_bootstrap/') else ('workspace_provisioning',name.split('/',1)[1]))
 result[name]=hashlib.sha256((roots[package]/relative).read_bytes()).hexdigest()
print(json.dumps(result))
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
                "-i",
                "--entrypoint",
                "/opt/executor/bin/python",
                image,
                "-B",
                "-c",
                code,
            ],
            input=json.dumps(names),
            text=True,
            timeout=120,
        )
    )


def collect(
    enrollment,
    original,
    descriptor,
    session,
    *,
    read_kube=kubectl,
    read_sources=image_sources,
    historical_archives=None,
    installer=None,
    archive_read_session=None,
    historical_environment=None,
    provenance_assumption=None,
):
    schema, boundary = contract(), source_contract()
    authority = schema.validate(enrollment["authority"])
    account = authority["account_id"]
    region = enrollment.get("management_region", authority["region"])
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
        original["status"] == "installed-and-verified"
        and original["installation_id"] == authority["installation_id"]
    )
    context = None
    if historical_archives is not None:
        from .bootstrap_history import qualify

        context = qualify(
            installer,
            enrollment,
            original,
            historical_archives,
            session,
            archive_read_session=archive_read_session,
        )
        environment = installer.env
    else:
        environment = original["environment"]
    require(
        environment["cluster"] == enrollment["management_cluster"]
        and environment["account_id"] == account
    )
    cluster = session.client("eks", region_name=region).describe_cluster(
        name=enrollment["management_cluster"]
    )["cluster"]
    configured = installed_config(enrollment["gateway_namespace"], cluster)
    table = f"adp-{enrollment['environment']}-superplane-provider-authorities"
    require(configured["ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE"] == table)
    ddb = session.client("dynamodb", region_name=region)

    def enrolled():
        item = ddb.get_item(
            TableName=table,
            Key={"record_id": {"S": authority["credential_id"]}},
            ConsistentRead=True,
        ).get("Item")
        require(
            item is not None
            and item["document"]["S"] == schema.canonical(authority)
            and item["revision"]["S"] == schema.digest(authority)
        )

    enrolled()
    environment_history = None
    if historical_environment is not None:
        from .bootstrap_environment_history import HistoricalEnvironment

        require(historical_archives is None)
        environment_history = HistoricalEnvironment(
            installer, enrollment, original, descriptor, session, historical_environment
        )
    namespace = environment["namespace"]

    def obj(kind, name):
        return json.loads(read_kube("-n", namespace, "get", kind, name, "-o", "json"))

    require(
        obj("scaledjob", "superplane-paid-worker")["metadata"]["annotations"][
            "autoscaling.keda.sh/paused"
        ]
        == "true"
    )
    api = obj("deployment", "superplane-api")
    container = next(
        c
        for c in api["spec"]["template"]["spec"]["containers"]
        if c["name"] == "superplane-api"
    )
    require(
        next(
            e["value"]
            for e in container["env"]
            if e["name"] == "SUPERPLANE_OPERATION_DISPATCH_ENABLED"
        )
        == "false"
    )
    pods = json.loads(
        read_kube(
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            "scaledjob.keda.sh/name=superplane-paid-worker",
            "-o",
            "json",
        )
    )["items"]
    require(all(p["status"]["phase"] in {"Succeeded", "Failed"} for p in pods))
    require(all(descriptor[k] == authority[k] for k in ("org_id", "workspace_id")))
    require(descriptor["source_pod_uid"] == descriptor["workload_binding"])
    if provenance_assumption is not None:
        from .bootstrap_diagnostic_assumption import ApprovedDiagnosticAssumption

        require(type(provenance_assumption) is ApprovedDiagnosticAssumption)
        provenance_assumption.bind_descriptor(descriptor)
    now = datetime.now(timezone.utc)
    pod, audit = source_pod(
        session,
        cluster=enrollment["management_cluster"],
        namespace=namespace,
        name=descriptor["source_pod_name"],
        uid=descriptor["source_pod_uid"],
        acquired_at=datetime.fromisoformat(descriptor["source_acquired_at"]),
        now=now,
        management_region=region,
        historical_bootstrap=context is not None,
        bootstrap_diagnostics=True,
        environment_history=environment_history,
        provenance_assumption=provenance_assumption,
    )
    worker = pod["spec"]["containers"][0]
    image = worker["image"]
    source = boundary.qualified_source(image)
    require(source is not None)
    require(read_sources(image, list(source["hashes"])) == source["hashes"])
    mount = next(m for m in worker["volumeMounts"] if m["mountPath"] == "/run/policy")
    volume = next(v for v in pod["spec"]["volumes"] if v["name"] == mount["name"])
    require(
        mount.get("readOnly") is True
        and not mount.get("subPath")
        and not mount.get("subPathExpr")
    )
    require(
        [
            e.get("value")
            for e in worker["env"]
            if e["name"] == "SUPERPLANE_LIFECYCLE_POLICY_FILE"
        ]
        == ["/run/policy/lifecycle.json"]
    )
    policy = obj("configmap", volume["configMap"]["name"])
    require(
        policy.get("immutable") is True
        and policy["metadata"]["creationTimestamp"]
        <= pod["metadata"]["creationTimestamp"]
    )
    from workspace_provisioning.lifecycle_policy import policy_digest, select_policy

    selected = select_policy(
        json.loads(policy["data"]["lifecycle.json"]),
        authority["org_id"],
        authority["workspace_id"],
    )
    require(
        selected.runtime.get("management_public_access")
        and selected.runtime["workspace_variables"].get("networking_mode", "owned")
        == "owned"
    )
    identity = {
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
    enrolled()
    if environment_history is not None:
        environment_history.finish()
    if context is not None:
        require(
            qualify(
                installer,
                enrollment,
                original,
                historical_archives,
                session,
                archive_read_session=archive_read_session,
            )
            == context
        )
        installer.receipt["bootstrap_historical_context"] = context
        installer.save()
    result = {
        **identity,
        "version": 1,
        "record_id": "sppr1:bootstrap:" + schema.digest(identity),
        "evidence_kind": "bootstrap-journal-barrier-v1",
        **{
            k: authority[k]
            for k in (
                "credential_id",
                "installation_id",
                "adp_org_id",
                "account_id",
                "region",
            )
        },
        **{
            k: descriptor[k]
            for k in (
                "source_principal",
                "source_acquired_at",
                "source_pod_name",
                "source_pod_uid",
                "workload_binding",
            )
        },
        "source_image": image,
        "source_revision": source["revision"],
        "source_hashes": source["hashes"],
        "source_audit": audit,
        **(
            {"provenance_assumption": audit["provenance_assumption"]}
            if provenance_assumption is not None
            else {}
        ),
        "policy_sha256": policy_digest(selected),
        "policy_configmap_sha256": hashlib.sha256(
            policy["data"]["lifecycle.json"].encode()
        ).hexdigest(),
        "checked_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }
    if "settled_bootstrap" in descriptor:
        from harness_jobs.settled_bootstrap import validate

        closure = validate(descriptor["settled_bootstrap"])
        require(
            all(
                closure["operation"][k] == descriptor[k]
                for k in ("operation_id", "org_id", "workspace_id", "plan_digest")
            )
        )
        require(
            closure["source_audit"]["recorded_at"] == descriptor["source_acquired_at"]
        )
        require(closure["call"]["attempt_id"] == descriptor["source_principal"])
        result["settled_bootstrap"] = closure
    return result


def evidence_contract():
    path = (
        Path(__file__).resolve().parents[4]
        / "modules/gateway/src/shared/domain_bootstrap_evidence.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_evidence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(
    installer,
    enrollment,
    original,
    descriptor,
    session,
    *,
    check_only=True,
    evidence_collector=collect,
):
    """Publish one immutable qualification under the genuine native owner lock.

    Each native run persists its exact intent before PutItem. An uncertain prior
    intent is observe-only: a rerun cannot recollect, replace it, or resend it.
    """
    require(
        installer.receipt.get("remote_lock")
        and installer.owner == enrollment["authority"]["installation_id"]
    )
    lock = installer.reconcile_lock_attempt()
    require(lock is not None)

    def locked():
        require(installer.reconcile_lock_attempt() == lock)

    schema = evidence_contract()
    identity = {key: descriptor[key] for key in schema.IDENTITY}
    table = f"adp-{enrollment['environment']}-superplane-provider-authorities"
    client = session.client(
        "dynamodb",
        region_name=enrollment.get(
            "management_region", enrollment["authority"]["region"]
        ),
    )

    def read_item(key):
        return client.get_item(
            TableName=table, Key={"record_id": {"S": key}}, ConsistentRead=True
        ).get("Item")

    intent = installer.receipt.get("bootstrap_recovery_publication_intent")
    if intent is not None:
        # Never reinterpret a lost acknowledgement as permission for a new write.
        require(
            isinstance(intent, dict)
            and set(intent) == {"version", "identity", "table", "item"}
            and type(intent["version"]) is int
            and intent["version"] == 1
            and intent["identity"] == identity
            and intent["table"] == table
        )
        item = intent["item"]
        evidence = schema.decode_item(item, item["record_id"]["S"])
        require(all(evidence[key] == identity[key] for key in schema.IDENTITY))
        require(read_item(evidence["record_id"]) == item)
        records = schema.chain(read_item, identity)
        require(records and records[-1] == evidence)
    else:
        # A completed legacy receipt belongs to its original publication.
        require(installer.receipt.get("bootstrap_recovery_evidence") is None)
        records = schema.chain(read_item, identity)
        if records:
            require(
                datetime.fromisoformat(records[-1]["expires_at"])
                <= datetime.now(timezone.utc)
            )
            require(schema.generation(records[-1]) < schema.MAX_RENEWALS)
        # Fresh collection rechecks the stopped source, absence and native state.
        evidence = evidence_collector(enrollment, original, descriptor, session)
        require(all(evidence[key] == identity[key] for key in schema.IDENTITY))
        if records:
            evidence = schema.renew(evidence, records[-1])
        else:
            schema.framing(evidence)
            require(schema.generation(evidence) == 0)
        locked()
        require(schema.chain(read_item, identity) == records)
        if not check_only:
            item = {
                "record_id": {"S": evidence["record_id"]},
                "document": {"S": schema.encode_document(evidence)},
                "revision": {"S": schema.digest(evidence)},
            }
            installer.receipt["bootstrap_recovery_publication_intent"] = copy.deepcopy(
                {
                    "version": 1,
                    "identity": identity,
                    "table": table,
                    "item": item,
                }
            )
            installer.save()
            locked()
            require(schema.chain(read_item, identity) == records)
            try:
                client.put_item(
                    TableName=table,
                    Item=item,
                    ConditionExpression="attribute_not_exists(record_id)",
                )
            except Exception:
                # A competing different writer must fail, even at the same key.
                require(read_item(evidence["record_id"]) == item)
            require(read_item(evidence["record_id"]) == item)
            require(schema.chain(read_item, identity) == [*records, evidence])
    schema.framing(evidence)
    locked()
    if check_only:
        return {
            "state": "bootstrap-boundary-reviewed",
            "human_admitted": False,
            "evidence": evidence,
        }
    previous = installer.receipt.get("bootstrap_recovery_evidence")
    require(previous is None or previous == evidence)
    installer.receipt["bootstrap_recovery_evidence"] = copy.deepcopy(evidence)
    installer.save()
    return {
        "state": "bootstrap-boundary-recorded",
        "record_id": evidence["record_id"],
        "human_admitted": False,
    }
