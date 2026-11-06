"""Finite qualification of four inspected diagnostic sessions on one old worker.

This is source provenance, never new execution permission. The caller has read
the complete protected source history through actual now. The fixed records
identify only reviewed Python programs without a mutation mechanism; stream
upgrade status does not prove successful execution or the contents of stdin.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .provider_authority import require


ROOT = Path(__file__).with_name("evidence") / "bootstrap-diagnostics-20261028"
COMPONENT_ROOT = Path(__file__).with_name("evidence") / "bootstrap-diagnostics-20261102"


def component_source(cluster, namespace, name):
    """Select only the separately reviewed management-component worker."""
    from .bootstrap_current_diagnostics import matches

    return matches(cluster, namespace, name) or (cluster, namespace, name) == (
        "adp-dev-eks-cluster",
        "superplane",
        "superplane-paid-worker-6nf6p-2gs8d",
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def mutation_identity(value):
    """Credential-free API evidence for the finite pod mutation history."""
    return digest(
        {
            key: value.get(key)
            for key in (
                "auditID",
                "stage",
                "verb",
                "objectRef",
                "responseStatus",
                "requestObject",
                "responseObject",
                "requestReceivedTimestamp",
                "stageTimestamp",
            )
        }
    )


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None)
    return result


def environment(session, known, *, created, management_region, precise=False):
    """Bind envFrom imports while the original runtime ConfigMap is retained.

    Kubernetes audit logs omit ConfigMap data, so the still-current object plus
    complete no-mutation history establishes its value at container start. This
    must run before rolling the worker ConfigMap to the replacement runtime.
    """
    from .planning_recovery import kubectl

    expected = known["environment_configmap"]

    def read():
        return json.loads(
            kubectl(
                "-n",
                expected["namespace"],
                "get",
                "configmap",
                expected["name"],
                "-o",
                "json",
            )
        )

    before = read()
    metadata = before["metadata"]
    require(
        metadata["name"] == expected["name"]
        and metadata["namespace"] == expected["namespace"]
        and metadata["uid"] == expected["uid"]
        and metadata["resourceVersion"] == expected["resource_version"]
        and instant(metadata["creationTimestamp"]) < created
        and digest(before["data"]) == expected["data_sha256"]
        and not any(key.startswith("PYTHON") for key in before["data"])
    )
    client = session.client(
        "logs", **({"region_name": management_region} if management_region else {})
    )
    now, count, reads = datetime.now(timezone.utc), 0, 0
    pattern = json.dumps(expected["name"])
    if precise:
        pattern = (
            '{ $.objectRef.resource = "configmaps" && $.objectRef.namespace = '
            + json.dumps(expected["namespace"])
            + " && ($.objectRef.name = "
            + json.dumps(expected["name"])
            + " || $.responseObject.metadata.name = "
            + json.dumps(expected["name"])
            + ") }"
        )
    for page in client.get_paginator("filter_log_events").paginate(
        logGroupName=f"/aws/eks/{known['cluster']}/cluster",
        logStreamNamePrefix="kube-apiserver-audit-",
        startTime=int((created - timedelta(minutes=5)).timestamp() * 1000),
        endTime=int(now.timestamp() * 1000),
        filterPattern=pattern,
    ):
        for event in page.get("events", []):
            count += 1
            require(
                count <= 10000
                and event["logStreamName"].startswith("kube-apiserver-audit-")
            )
            value = json.loads(event["message"])
            ref = value.get("objectRef", {})
            if (
                ref.get("resource") == "configmaps"
                and ref.get("namespace") == expected["namespace"]
            ):
                require(value.get("verb") in {"get", "list", "watch"})
                reads += 1
    require(reads > 0 and read() == before)


def qualify(
    events,
    *,
    cluster,
    pod,
    creation_event,
    terminals,
    session,
    management_region=None,
    environment_history=None,
    audit=None,
):
    """Accept the fixed complete audit set, with unchanged source and operator."""
    metadata = pod["metadata"]
    from .bootstrap_current_diagnostics import matches, qualify as current_qualify

    if matches(cluster, metadata["namespace"], metadata["name"]):
        current_qualify(
            events,
            cluster=cluster,
            pod=pod,
            creation_event=creation_event,
            terminals=terminals,
            session=session,
            management_region=management_region,
            environment_history=environment_history,
        )
        return None
    components = component_source(cluster, metadata["namespace"], metadata["name"])
    root = COMPONENT_ROOT if components else ROOT
    known = json.loads((root / "qualification.json").read_text())
    require(not components or environment_history is None)
    actual = [
        (event, value)
        for event, value in events
        if value.get("objectRef", {}).get("resource") == "pods"
        and value.get("objectRef", {}).get("subresource")
        in {"exec", "attach", "ephemeralcontainers"}
    ]
    selected = (cluster, metadata["namespace"], metadata["name"], metadata["uid"]) == (
        known["cluster"],
        known["namespace"],
        known["pod_name"],
        known["pod_uid"],
    )
    require(not components or selected)
    if not selected and not actual:
        require(environment_history is None)
        return  # Ordinary bootstrap sources with no diagnostic session.
    require(known["version"] == 1 and selected)
    require(
        (metadata["namespace"], metadata["name"], metadata["uid"])
        == (known["namespace"], known["pod_name"], known["pod_uid"])
        and creation_event["eventId"] == known["creation_event_id"]
    )
    require(len(pod["spec"]["containers"]) == 1)
    worker = pod["spec"]["containers"][0]
    require(
        worker["image"] == known["image"]
        and digest(worker) == known["container_sha256"]
        and digest(pod["spec"]["volumes"]) == known["pod_volumes_sha256"]
        and worker["securityContext"]["readOnlyRootFilesystem"] is True
    )
    created = instant(metadata["creationTimestamp"])
    ended = min(
        instant(json.loads(event["message"])["stageTimestamp"])
        for event, _ in terminals
    )
    expected = {
        stage["event_id"]: (execution, stage)
        for execution in known["execs"]
        for stage in execution["stages"]
    }
    require(
        len(expected) == (110 if components else 8)
        and len(known["execs"]) == (55 if components else 4)
    )
    require(
        len(actual) == len(expected)
        and {event["eventId"] for event, _ in actual} == set(expected)
    )
    if components:
        mutations = [
            (event, value)
            for event, value in events
            if value.get("verb") not in {"get", "list", "watch"}
        ]
        require(
            len(mutations) == len(known["mutations"]) == 8
            and {event["eventId"] for event, _ in mutations} == set(known["mutations"])
            and all(
                mutation_identity(value) == known["mutations"][event["eventId"]]
                for event, value in mutations
            )
        )
    # An exec ref may omit UID. The unique creation and all pod observations
    # must describe this same lifetime, and every stream falls inside it.
    for _, value in events:
        response = value.get("responseObject", {})
        observed = response.get("metadata", {})
        if observed.get("name") == known["pod_name"]:
            require(observed.get("uid") == known["pod_uid"])
            if components and response.get("kind") == "Pod":
                require(
                    response["spec"]["containers"] == pod["spec"]["containers"]
                    and response["spec"]["volumes"] == pod["spec"]["volumes"]
                    and not response["spec"].get("ephemeralContainers")
                )
    for event, value in actual:
        execution, stage = expected[event["eventId"]]
        require(
            value.get("auditID") == execution["audit_id"]
            and value.get("stage") == stage["stage"]
            and value.get("verb") == "get"
            and value.get("responseStatus", {}).get("code") == 101
            and value.get("requestReceivedTimestamp") == execution["time"]
            and value.get("stageTimestamp") == stage["stage_timestamp"]
            and created
            < instant(execution["time"])
            <= instant(stage["stage_timestamp"])
            < ended
            and value.get("objectRef")
            == {
                "resource": "pods",
                "namespace": known["namespace"],
                "name": known["pod_name"],
                "apiVersion": "v1",
                "subresource": "exec",
            }
        )
        user = value.get("user", {})
        extra = user.get("extra", {})
        actor = known["actors"][execution["actor"]] if components else execution
        require(
            user.get("username") == actor["user_arn"]
            and extra.get("arn") == [actor["user_arn"]]
            and extra.get("principalId") == actor["principal_id"]
            and extra.get("canonicalArn") == actor["canonical_arn"]
            and extra.get("sessionName") == [actor["user_arn"].rsplit("/", 1)[1]]
            and value.get("sourceIPs") == actor["source_ips"]
            and value.get("userAgent") == actor["user_agent"]
        )
        uri = value.get("requestURI", "")
        require(
            hashlib.sha256(uri.encode()).hexdigest() == execution["request_uri_sha256"]
        )
        parsed = urlsplit(uri)
        require(
            not parsed.scheme
            and not parsed.netloc
            and not parsed.fragment
            and parsed.path
            == f"/api/v1/namespaces/{known['namespace']}/pods/{known['pod_name']}/exec"
        )
        query = parse_qs(parsed.query, strict_parsing=True)
        source = (root / known["sources"][execution["query_hash"]]).read_bytes()
        require(hashlib.sha256(source).hexdigest() == execution["query_hash"])
        require(
            query
            == {
                "command": [
                    *(
                        ["python", "-B"]
                        if components
                        else ["/opt/executor/bin/python", "-S"]
                    ),
                    "-c",
                    source.decode(),
                ],
                "container": ["paid-worker"],
                "stderr": ["true"],
                **({} if components else {"stdin": ["true"]}),
                "stdout": ["true"],
            }
        )
    if environment_history is not None:
        return environment_history.qualify(known, pod, created, ended, audit)
    environment(
        session,
        known,
        created=created,
        management_region=management_region,
        precise=components,
    )
