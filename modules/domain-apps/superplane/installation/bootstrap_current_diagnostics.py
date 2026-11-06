"""Exact protected diagnostic programs on the stopped partial-reader worker.

No code is accepted from caller input or stdin. This profile qualifies only the
recorded program/actor/pod history; stream responses do not prove completion.
"""

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .provider_authority import require

ROOT = Path(__file__).with_name("evidence") / "bootstrap-diagnostics-20261104"
MANIFEST_SHA256 = "e01b4158aaef42084864f55b7742ad5286ae314a13a7b485ed8cb33cf00e7a58"
IDENTITY = (
    "adp-dev-eks-cluster",
    "superplane",
    "superplane-paid-worker-4wslg-72lfx",
    "48c9ffb5-b2a9-49ba-b354-59cdd0fe4a98",
)


def matches(cluster, namespace, name):
    return (cluster, namespace, name) == IDENTITY[:3]


def profile():
    raw = (ROOT / "qualification.json").read_bytes()
    require(hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256)
    return json.loads(raw)


def verify_history(known, events, *, cluster, pod, creation_event, terminals):
    from .bootstrap_diagnostics import digest, instant, mutation_identity

    meta = pod["metadata"]
    require((cluster, meta["namespace"], meta["name"], meta["uid"]) == IDENTITY)
    require(
        tuple(known[k] for k in ("cluster", "namespace", "pod_name", "pod_uid"))
        == IDENTITY
        and known["version"] == 1
        and creation_event["eventId"] == known["creation_event_id"]
        and sorted(e["eventId"] for e, _ in terminals) == known["terminal_event_ids"]
    )
    spec = pod["spec"]
    require(
        len(spec["containers"]) == 1
        and not spec.get("initContainers")
        and not spec.get("ephemeralContainers")
    )
    worker = spec["containers"][0]
    require(
        worker["image"] == known["image"]
        and digest(worker) == known["container_sha256"]
        and digest(spec["volumes"]) == known["pod_volumes_sha256"]
        and worker["securityContext"]["readOnlyRootFilesystem"] is True
    )
    hashes = known["protected_event_hashes"]
    require(
        len(events) == len(hashes) == 41
        and {e["eventId"] for e, _ in events} == set(hashes)
        and all(
            hashlib.sha256(e["message"].encode()).hexdigest() == hashes[e["eventId"]]
            for e, _ in events
        )
    )
    mutations = [
        (e, v) for e, v in events if v.get("verb") not in {"get", "list", "watch"}
    ]
    require(
        len(mutations) == len(known["mutations"]) == 7
        and {e["eventId"] for e, _ in mutations} == set(known["mutations"])
        and all(
            mutation_identity(v) == known["mutations"][e["eventId"]]
            for e, v in mutations
        )
    )
    expected = {
        s["event_id"]: (run, s) for run in known["execs"] for s in run["stages"]
    }
    actual = [
        (e, v)
        for e, v in events
        if v.get("objectRef", {}).get("subresource")
        in {"exec", "attach", "ephemeralcontainers"}
    ]
    require(len(known["execs"]) == 17 and len(expected) == len(actual) == 34)
    require({e["eventId"] for e, _ in actual} == set(expected))
    created = instant(meta["creationTimestamp"])
    ended = min(
        instant(json.loads(e["message"])["stageTimestamp"]) for e, _ in terminals
    )
    for event, value in actual:
        run, stage = expected[event["eventId"]]
        uri = value["requestURI"]
        parsed = urlsplit(uri)
        require(
            value["auditID"] == run["audit_id"]
            and value["verb"] == "get"
            and value["stage"] == stage["stage"]
            and value["stage"] in {"ResponseStarted", "ResponseComplete"}
            and value["responseStatus"]["code"] == stage["response_code"] == 101
            and value["requestReceivedTimestamp"] == run["time"]
            and value["stageTimestamp"] == stage["stage_timestamp"]
            and created
            < instant(run["time"])
            <= instant(stage["stage_timestamp"])
            < ended
            and digest(value["user"]) == run["actor_sha256"]
            and value["sourceIPs"] == run["source_ips"]
            and value["userAgent"] == run["user_agent"]
            and hashlib.sha256(uri.encode()).hexdigest() == run["uri_sha256"]
            and not parsed.scheme
            and not parsed.netloc
            and not parsed.fragment
            and parsed.path
            == f"/api/v1/namespaces/{IDENTITY[1]}/pods/{IDENTITY[2]}/exec"
        )
        program = (ROOT / known["sources"][run["program_sha256"]]).read_bytes()
        require(hashlib.sha256(program).hexdigest() == run["program_sha256"])
        require(
            parse_qs(parsed.query, strict_parsing=True)
            == {
                **run["query_without_command"],
                "command": [*run["command_prefix"], program.decode()],
            }
        )
    for _, value in events:
        response = value.get("responseObject", {})
        if response.get("kind") == "Pod":
            require(response["metadata"]["uid"] == IDENTITY[3])
            require(response["spec"]["containers"] == spec["containers"])
            require(response["spec"]["volumes"] == spec["volumes"])
            require(not response["spec"].get("ephemeralContainers"))
    return created


def qualify(
    events,
    *,
    cluster,
    pod,
    creation_event,
    terminals,
    session,
    management_region,
    environment_history,
):
    from .bootstrap_diagnostics import environment

    require(environment_history is None)
    known = profile()
    created = verify_history(
        known,
        events,
        cluster=cluster,
        pod=pod,
        creation_event=creation_event,
        terminals=terminals,
    )
    environment(
        session,
        known,
        created=created,
        management_region=management_region,
        precise=True,
    )
