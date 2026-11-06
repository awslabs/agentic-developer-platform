import runpy
from pathlib import Path

import pytest

render = runpy.run_path(str(Path(__file__).resolve().parents[1] / "k8s" / "render.py"))["render"]


def arguments(configuration):
    return {
        "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tool-adapters@" + configuration.release.image_digest,
        "namespace": "adp-tools",
        "role_arn": "arn:aws:iam::123456789012:role/adp-dev-tool-adapters",
        "configuration": configuration,
    }


def test_service_is_separate_private_and_hardened(configuration):
    items = {item["kind"]: item for item in render(**arguments(configuration))["items"]}
    assert set(items) == {"ServiceAccount", "ConfigMap", "Deployment", "Service", "PodDisruptionBudget"}
    pod = items["Deployment"]["spec"]["template"]["spec"]
    assert items["Deployment"]["spec"]["replicas"] == 2
    assert pod["serviceAccountName"] == "tool-adapters"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    assert {volume["name"] for volume in pod["volumes"]} == {"configuration", "scratch"}
    assert not any("secret" in volume for volume in pod["volumes"])
    assert items["Service"]["spec"]["type"] == "ClusterIP"
    assert items["PodDisruptionBudget"]["spec"]["minAvailable"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "image:latest"),
        ("image", "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tool-adapters@sha256:" + "c" * 64),
        ("role_arn", "arn:aws:iam::999999999999:role/other"),
        ("namespace", "bad/namespace"),
    ],
)
def test_unpinned_or_mismatched_release_is_refused(configuration, field, value):
    with pytest.raises(ValueError):
        render(**{**arguments(configuration), field: value})


def test_configuration_change_rolls_the_pods(configuration):
    changed = configuration.model_copy(
        update={"release": configuration.release.model_copy(update={"backend_generation": 2})}
    )

    def annotation(config):
        deployment = next(item for item in render(**arguments(config))["items"] if item["kind"] == "Deployment")
        return deployment["spec"]["template"]["metadata"]["annotations"]["checksum/adp-adapter-contract"]

    assert annotation(configuration) != annotation(changed)
