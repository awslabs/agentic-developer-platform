import copy

import pytest

from installation.config import LABEL, Refusal, identity
from installation.job_sidecar_recovery import observation
from installation.manifests import migration_job, render


@pytest.fixture
def retained(environment, release):
    environment["recovery_transport"] = {
        "tls_secret": "adp/dev/superplane/recovery-tls",
        "credential_secret": "adp/dev/superplane/recovery-credential",
    }
    job = migration_job(environment, release, "retained")
    api = next(
        d
        for d in render(environment, release)
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "superplane-api"
    )
    job["spec"]["template"]["spec"]["containers"].append(
        api["spec"]["template"]["spec"]["containers"][-1]
    )
    job["metadata"]["uid"] = "job-uid"
    pod = copy.deepcopy(job["spec"]["template"])
    pod["metadata"].update(
        name="migration-pod",
        namespace=environment["namespace"],
        uid="pod-uid",
        ownerReferences=[{"controller": True, "kind": "Job", "uid": "job-uid"}],
    )
    pod["status"] = {
        "containerStatuses": [
            {
                "name": "migrate",
                "restartCount": 0,
                "state": {"terminated": {"exitCode": 0, "containerID": "main-id"}},
            },
            {
                "name": "recovery-transport",
                "restartCount": 0,
                "containerID": "listener-id",
                "state": {"running": {"startedAt": "now"}},
            },
        ]
    }
    receipt = {
        "run_id": "retained",
        "environment": environment,
        "release_lock": release,
        "installation_id": identity(environment),
        "objects": [
            {
                "kind": "Job",
                "name": job["metadata"]["name"],
                "namespace": environment["namespace"],
                "uid": "job-uid",
            }
        ],
    }
    assert pod["metadata"]["labels"][LABEL] == receipt["installation_id"]
    return receipt, job, pod


def test_recovery_observes_success_without_mutating_receipt(retained):
    before = copy.deepcopy(retained)
    value = observation(*retained, "migrate")
    assert value["main_result"]["exitCode"] == 0
    assert value["listener_container_id"] == "listener-id"
    assert retained == before


@pytest.mark.parametrize(
    "change",
    [
        "failed-main",
        "running-main",
        "restarted-main",
        "foreign-job",
        "foreign-pod",
        "wrong-image",
        "wrong-command",
        "terminal-job",
        "unrecorded-job",
        "extra-container",
    ],
)
def test_refuses_unsafe_listener_shutdown(retained, change):
    receipt, job, pod = retained
    main = pod["status"]["containerStatuses"][0]
    if change == "failed-main":
        main["state"]["terminated"]["exitCode"] = 1
    elif change == "running-main":
        main["state"] = {"running": {}}
    elif change == "restarted-main":
        main["restartCount"] = 1
    elif change == "foreign-job":
        job["metadata"]["uid"] = "replacement"
    elif change == "foreign-pod":
        pod["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    elif change == "wrong-image":
        pod["spec"]["containers"][1]["image"] = "unreviewed:latest"
    elif change == "wrong-command":
        pod["spec"]["containers"][1]["command"] = ["python", "-m", "another-service"]
    elif change == "terminal-job":
        job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    elif change == "unrecorded-job":
        receipt["objects"] = []
    elif change == "extra-container":
        pod["spec"]["containers"].append({"name": "another-service"})
    with pytest.raises(Refusal):
        observation(receipt, job, pod, "migrate")
