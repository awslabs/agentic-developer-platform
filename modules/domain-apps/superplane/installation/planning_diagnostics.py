"""Qualify only three maintained process-read programs in protected pod audits.

No exec replay, stdin program, arbitrary Python, attach or ephemeral container is
accepted. Successful upgrade does not prove program success; the fixed programs
have no mutation or credential-reading operation even if interrupted.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .provider_authority import require


def instant(value):
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None)
    return result


def environment(session, cluster, pod, region):
    from .planning_recovery import kubectl

    container = pod["spec"]["containers"][0]
    require(
        not any(
            e["name"].startswith("PYTHON") or e["name"] == "PATH"
            for e in container.get("env", [])
        )
    )
    require(not container.get("workingDir"))
    require(container.get("securityContext", {}).get("readOnlyRootFilesystem") is True)
    require(
        all(
            m["mountPath"].startswith(("/run/", "/tmp", "/var/run/"))
            for m in container.get("volumeMounts", [])
        )
    )
    created = instant(pod["metadata"]["creationTimestamp"])
    client = session.client("logs", **({"region_name": region} if region else {}))
    result = []
    for source in container.get("envFrom", []):
        require(
            set(source) == {"configMapRef"} and set(source["configMapRef"]) == {"name"}
        )
        name, namespace = source["configMapRef"]["name"], pod["metadata"]["namespace"]

        def read(namespace=namespace, name=name):
            return json.loads(
                kubectl("-n", namespace, "get", "configmap", name, "-o", "json")
            )

        before = read()
        require(instant(before["metadata"]["creationTimestamp"]) < created)
        require(
            not any(
                k.startswith("PYTHON") or k == "PATH" for k in before.get("data", {})
            )
        )
        count = 0
        for page in client.get_paginator("filter_log_events").paginate(
            logGroupName=f"/aws/eks/{cluster}/cluster",
            logStreamNamePrefix="kube-apiserver-audit-",
            startTime=int((created - timedelta(minutes=5)).timestamp() * 1000),
            endTime=int(datetime.now(UTC).timestamp() * 1000),
            filterPattern=json.dumps(name),
        ):
            for event in page.get("events", []):
                count += 1
                require(count <= 10000)
                value = json.loads(event["message"])
                ref = value.get("objectRef", {})
                if (ref.get("resource"), ref.get("namespace"), ref.get("name")) == (
                    "configmaps",
                    namespace,
                    name,
                ):
                    require(value.get("verb") in {"get", "list", "watch"})
        require(count > 0 and read() == before)
        result.append(
            {
                "name": name,
                "uid": before["metadata"]["uid"],
                "data_sha256": hashlib.sha256(
                    json.dumps(before.get("data", {}), sort_keys=True).encode()
                ).hexdigest(),
            }
        )
    return result


def qualify(events, *, cluster, pod, terminals, session, management_region, operator):
    actual = [
        (event, value)
        for event, value in events
        if value.get("objectRef", {}).get("resource") == "pods"
        and value.get("objectRef", {}).get("subresource")
        in {"exec", "attach", "ephemeralcontainers"}
    ]
    if not actual:
        return {"executions": []}
    require(len(actual) <= 32)
    programs = {
        (Path(__file__).with_name("diagnostics") / (name + ".txt")).read_text()
        for name in ("process_names", "process_service", "process_stage")
    }
    metadata = pod["metadata"]
    container = pod["spec"]["containers"][0]
    created = instant(metadata["creationTimestamp"])
    ended = min(
        instant(
            v["status"]["containerStatuses"][0]["state"]["terminated"]["finishedAt"]
        )
        for _, v in terminals
        if v.get("status", {})
        .get("containerStatuses", [{}])[0]
        .get("state", {})
        .get("terminated")
    )
    groups = {}
    for event, value in actual:
        ref = value["objectRef"]
        require(
            ref.get("subresource") == "exec"
            and ref.get("name") == metadata["name"]
            and ref.get("namespace") == metadata["namespace"]
        )
        require(
            value.get("verb") == "get"
            and value.get("responseStatus", {}).get("code") == 101
        )
        query = parse_qs(urlsplit(value["requestURI"]).query, keep_blank_values=True)
        command = query.pop("command", [])
        require(
            len(command) == 3
            and command[:2] == ["python", "-c"]
            and command[2] in programs
        )
        require(
            query
            == {
                "container": [container["name"]],
                "stderr": ["true"],
                "stdout": ["true"],
            }
        )
        user = value.get("user", {})
        require(
            user.get("extra", {}).get("canonicalArn") == [operator["arn"]]
            and user.get("extra", {}).get("principalId") == [operator["role_id"]]
        )
        require(
            created
            <= instant(value["requestReceivedTimestamp"])
            <= instant(value["stageTimestamp"])
            <= ended
        )
        groups.setdefault(value["auditID"], []).append((event, value, command[2]))
    records = []
    for audit_id, pair in groups.items():
        require(
            len(pair) == 2
            and {v["stage"] for _, v, _ in pair}
            == {"ResponseStarted", "ResponseComplete"}
        )
        require(
            pair[0][2] == pair[1][2]
            and pair[0][1]["requestURI"] == pair[1][1]["requestURI"]
        )
        records.append(
            {
                "audit_id": audit_id,
                "event_ids": sorted(e["eventId"] for e, _, _ in pair),
                "program_sha256": hashlib.sha256(pair[0][2].encode()).hexdigest(),
            }
        )
    return {
        "executions": records,
        "environment": environment(session, cluster, pod, management_region),
    }
