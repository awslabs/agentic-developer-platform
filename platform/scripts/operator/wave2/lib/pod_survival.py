"""Derive pause survival from operator-collected Kubernetes observations."""
from datetime import datetime


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation timestamps must have a timezone")
    return parsed


def observe_survival(observation):
    before = observation["before"]
    after = observation["after"]
    result = observation["result"]
    uid = before["metadata"]["uid"]
    if not uid or after.get("pod_uid") != uid or result.get("pod_uid") != uid:
        raise ValueError("survival observations belong to different pods")
    if after.get("returncode") != 0 or after.get("absent"):
        raise ValueError("no successful post-experiment pod observation")
    collected = timestamp(observation["result_collected_at"])
    observed = timestamp(after["observed_at"])
    if observed < collected:
        raise ValueError("pod observation predates experiment result collection")

    def worker(status):
        rows = [c for c in status["containerStatuses"] if c["name"] == "agent-worker"]
        if len(rows) != 1:
            raise ValueError("expected exactly one agent-worker container")
        return rows[0]

    first, last = worker(before["status"]), worker(after["status"])
    if not first.get("state", {}).get("running") or not first.get("ready"):
        raise ValueError("worker was not running and ready before handoff")
    started = timestamp(first["state"]["running"]["startedAt"])
    if started > collected:
        raise ValueError("result collection predates worker start")
    for container in (first, last):
        if type(container.get("restartCount")) is not int or not container.get("containerID"):
            raise ValueError("missing restart count or container identity")
    killed = (last["restartCount"] != first["restartCount"]
              or last["containerID"] != first["containerID"])
    terminal = last.get("state", {}).get("terminated")
    if terminal:
        killed |= timestamp(terminal["finishedAt"]) < collected
        killed |= terminal.get("reason") == "OOMKilled"
    elif not last.get("state", {}).get("running"):
        raise ValueError("worker has neither running nor terminated observation")
    if after.get("deletion_timestamp"):
        killed |= timestamp(after["deletion_timestamp"]) < collected
    events = observation["events"]
    if not isinstance(events.get("items"), list):
        raise ValueError("missing Kubernetes event list")
    for event in events["items"]:
        if event.get("involvedObject", {}).get("uid") != uid:
            raise ValueError("event belongs to another pod")
        # Conservatively reject any kill observation for this fixture UID.
        killed |= event.get("reason") in {"Killing", "OOMKilled", "Evicted", "Preempted"}
    return {"pod_killed": bool(killed), "pod_uid": uid,
            "container_id_before": first["containerID"], "container_id_after": last["containerID"],
            "restart_count_before": first["restartCount"], "restart_count_after": last["restartCount"],
            "result_collected_at": observation["result_collected_at"],
            "observed_at": after["observed_at"]}
