"""Observe approved vocabulary images on this fixture's actual running pods."""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ownership import load_ledger
from worker_observation import ObservationError, digest_of, observe_worker_pod


def controlled_by(obj, kind, uid):
    return any(r.get("kind") == kind and r.get("uid") == uid and r.get("controller") is True
               for r in obj.get("metadata", {}).get("ownerReferences", []))


def observe(ledger, identity, deployment, replicasets, pods, worker, gateway_image, worker_image):
    gateway_digest, worker_digest = digest_of(gateway_image), digest_of(worker_image)
    if not gateway_digest or not worker_digest:
        raise ObservationError("approved gateway and worker images must carry exact digests")
    entries = [e for e in ledger["k8s"] if e["kind"] == "Deployment" and e["namespace"] == "adp-gateway"]
    if len(entries) != 1:
        raise ObservationError("ledger must identify exactly one fixture gateway Deployment")
    entry = entries[0]
    meta = deployment["metadata"]
    if any(meta.get(k) != entry[k] for k in ("name", "namespace", "uid")) or meta.get("deletionTimestamp"):
        raise ObservationError("gateway Deployment differs from the ledger or is terminating")
    for key in ("run_id", "account_id"):
        if identity.get(key) != ledger[key]:
            raise ObservationError(f"worker identity {key} differs from the ledger")
    if identity.get("nonce") != ledger["run_nonce"]:
        raise ObservationError("worker identity nonce differs from the ledger")
    for kind, uid in (("Pod", identity["pod_uid"]), ("Job", identity["job_uid"])):
        if not any(e["kind"] == kind and e["uid"] == uid for e in ledger["k8s"]):
            raise ObservationError(f"worker {kind} is not recorded in the ledger")
    if worker["metadata"].get("uid") != identity["pod_uid"]:
        raise ObservationError("worker pod was replaced")
    measured_worker = observe_worker_pod(
        worker, run_id=ledger["run_id"], nonce=ledger["run_nonce"],
        job_uid=identity["job_uid"], service_account=identity["service_account"],
        approved_digests=[worker_digest], container=identity["container"],
    )
    rs_uids = {r["metadata"]["uid"] for r in replicasets
               if controlled_by(r, "Deployment", entry["uid"])}
    owned = [p for p in pods if any(controlled_by(p, "ReplicaSet", uid) for uid in rs_uids)]
    if not owned:
        raise ObservationError("no gateway pods descend from the recorded Deployment")
    observations = []
    for pod in owned:
        metadata, status = pod["metadata"], pod.get("status", {})
        labels = metadata.get("labels", {})
        if (metadata.get("deletionTimestamp") or status.get("phase") != "Running"
                or labels.get("adp.io/w2-fixture") != ledger["run_id"]
                or labels.get("adp.io/w2-nonce") != ledger["run_nonce"]):
            raise ObservationError("gateway pod is not a running, nonterminating fixture pod")
        containers = status.get("containerStatuses", [])
        if len(containers) != 1 or containers[0].get("ready") is not True:
            raise ObservationError("gateway container is not uniquely identified and ready")
        if digest_of(containers[0].get("imageID", "")) != gateway_digest:
            raise ObservationError("gateway runtime digest differs from approved image")
        observations.append({"pod_uid": metadata["uid"], "image_id": containers[0]["imageID"]})
    return {"writer_digest_deployed": True, "gateway_digest_deployed": True,
            "run_id": ledger["run_id"], "run_nonce": ledger["run_nonce"],
            "gateway_deployment_uid": entry["uid"], "gateway_pods": observations,
            "worker": measured_worker, "approved_gateway_image": gateway_image,
            "approved_worker_image": worker_image}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("ledger", "identity", "gateway-image", "worker-image", "out"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    identity = json.loads(Path(args.identity).read_text())["expected_identity"]
    account = subprocess.check_output(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"], text=True).strip()
    if account != "879318057152":
        raise ObservationError("wrong AWS account")
    ledger = load_ledger(Path(args.ledger), run_id=identity["run_id"], account_id=account)
    entries = [e for e in ledger["k8s"] if e["kind"] == "Deployment" and e["namespace"] == "adp-gateway"]
    if len(entries) != 1:
        raise ObservationError("expected one fixture gateway")
    def get(kind, namespace, name=None):
        command = ["kubectl", "get", kind, "-n", namespace]
        if name:
            command.append(name)
        return json.loads(subprocess.check_output([*command, "-o", "json"], text=True))
    gateway = entries[0]
    result = observe(ledger, identity,
                     get("deployment", gateway["namespace"], gateway["name"]),
                     get("replicasets", gateway["namespace"])["items"],
                     get("pods", gateway["namespace"])["items"],
                     get("pod", identity["namespace"], identity["pod_name"]),
                     args.gateway_image, args.worker_image)
    result["observed_at"] = datetime.now(timezone.utc).isoformat()
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
