from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from installation.bootstrap_observer import KIND, render, reviewed
from installation.config import LABEL, Refusal, digest


def inputs():
    worker = {
        "kind": "ScaledJob",
        "metadata": {
            "name": "superplane-paid-worker",
            "namespace": "superplane",
            "labels": {LABEL: "owner"},
            "annotations": {"autoscaling.keda.sh/paused": "true"},
        },
        "spec": {
            "jobTargetRef": {
                "template": {
                    "metadata": {"labels": {"app": "worker"}},
                    "spec": {
                        "serviceAccountName": "original-worker",
                        "containers": [
                            {
                                "name": "paid-worker",
                                "image": "repo@sha256:" + "a" * 64,
                                "env": [],
                                "volumeMounts": [
                                    {"name": "state", "mountPath": "/run/state"}
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "state",
                                "persistentVolumeClaim": {"claimName": "retained"},
                            }
                        ],
                    },
                }
            }
        },
    }
    selector = {
        k: k for k in ("operation_id", "org_id", "workspace_id", "job_id", "attempt_id")
    }
    dispatch = {
        **selector,
        "selector": selector,
        "mode": "recovery",
        "domain": "superplane",
        "status": "pending",
        "observation_only": True,
        "source_fence": 11,
        "source_version": 6,
        "not_after": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }
    return worker, dispatch


def test_observer_preserves_entrypoint_network_identity_and_paused_runtime():
    worker, dispatch = inputs()
    before = deepcopy(worker)
    job = render(worker, dispatch, "owner", "a" * 24)
    pod = job["spec"]["template"]["spec"]
    assert worker == before
    assert pod["serviceAccountName"] == "original-worker"
    assert job["spec"]["template"]["metadata"]["labels"]["app"] == "worker"
    assert not pod["containers"][0].get("command") and not pod["containers"][0].get(
        "args"
    )
    assert pod["containers"][0]["volumeMounts"][0]["readOnly"] is True
    assert json.loads(pod["containers"][0]["env"][-1]["value"]) == dispatch["selector"]
    assert (
        job["spec"]["backoffLimit"] == 0 and job["spec"]["activeDeadlineSeconds"] == 600
    )


@pytest.mark.parametrize(
    "change", ["command", "identity", "execution", "expiry", "unpause", "extra"]
)
def test_changed_or_unbounded_observer_refused(change):
    worker, dispatch = inputs()
    if change == "command":
        worker["spec"]["jobTargetRef"]["template"]["spec"]["containers"][0][
            "command"
        ] = ["shell"]
    if change == "identity":
        worker["metadata"]["labels"][LABEL] = "foreign"
    if change == "execution":
        dispatch["mode"] = "execution"
    if change == "expiry":
        dispatch["not_after"] = "2020-01-01T00:00:00+00:00"
    if change == "unpause":
        worker["metadata"]["annotations"]["autoscaling.keda.sh/paused"] = "false"
    if change == "extra":
        dispatch["selector"]["new_authority"] = "admin"
    with pytest.raises(Refusal):
        render(worker, dispatch, "owner", "a" * 24)


def test_even_rehashed_saved_plan_cannot_change_job_role_or_entrypoint():
    worker, dispatch = inputs()
    i = SimpleNamespace(owner="owner", release="release")
    value = {
        "kind": KIND,
        "installation_id": "owner",
        "release_id": "release",
        "attempt": "a" * 24,
        "dispatch": dispatch,
        "before": [{}, worker],
        "job": render(worker, dispatch, "owner", "a" * 24),
    }
    reviewed(i, value, digest(value))
    for field, replacement in [("serviceAccountName", "admin"), ("hostNetwork", True)]:
        changed = deepcopy(value)
        changed["job"]["spec"]["template"]["spec"][field] = replacement
        with pytest.raises(Refusal):
            reviewed(i, changed, digest(changed))


def test_teardown_requires_terminal_original_uid_and_never_replays(
    monkeypatch, tmp_path
):
    from installation import bootstrap_observer as native

    calls = []
    i = SimpleNamespace(
        directory=tmp_path,
        env={"namespace": "superplane"},
        receipt={"remote_lock": {"etag": "held"}},
        kube=lambda *args, **kw: calls.append((args, kw)),
    )
    job = {
        "metadata": {
            "name": "original-observer",
            "uid": "original-uid",
            "resourceVersion": "17",
        }
    }
    terminal = {"terminal": False}
    monkeypatch.setattr(native, "observe", lambda *a: (terminal, job))
    monkeypatch.setattr(native, "objects", lambda *a: [])
    monkeypatch.setattr(native, "paused", lambda *a: None)
    monkeypatch.setattr(
        native, "verify_absence", lambda *a: {"observer_job_absent": True}
    )
    value = {"before": [], "job": job}
    with pytest.raises(Refusal, match="running Job"):
        native.teardown(i, value)
    assert not calls
    terminal["terminal"] = True
    assert native.teardown(i, value)["observer_job_absent"] is True
    args, kwargs = calls[0]
    assert args[:2] == ("delete", "--raw")
    options = json.loads(kwargs["data"])
    assert options["preconditions"] == {"uid": "original-uid", "resourceVersion": "17"}
    assert options["propagationPolicy"] == "Foreground"
    with pytest.raises(Refusal, match="previous deletion"):
        native.teardown(i, value)
    assert len(calls) == 1
