"""Declared successful ordinary Job Pod fixture, not live runtime evidence."""

from copy import deepcopy


def completed_job_pod(job):
    pod = deepcopy(job["spec"]["template"])
    pod["metadata"].update(
        name=job["metadata"]["name"] + "-pod",
        uid="completed-pod",
        namespace=job["metadata"]["namespace"],
        ownerReferences=[
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "name": job["metadata"]["name"],
                "uid": job["metadata"]["uid"],
                "controller": True,
                "blockOwnerDeletion": True,
            }
        ],
    )
    pod["spec"].update(nodeName="allocated-node", dnsPolicy="ClusterFirst")
    pod["status"] = {
        "phase": "Succeeded",
        "podIP": "10.1.2.3",
        "containerStatuses": [
            {
                "name": "workload",
                "state": {"terminated": {"exitCode": 0}},
            }
        ],
    }
    return pod
