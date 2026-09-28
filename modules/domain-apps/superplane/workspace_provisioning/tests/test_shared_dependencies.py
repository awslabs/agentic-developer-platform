"""Pinned dependency contract and actual read-only canonical network/schema proof."""

import base64
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants
from superplane_bootstrap.prerequisites import ExpectedPrerequisites

from workspace_provisioning.credential_controller.registry import Authority
from workspace_provisioning.shared_dependencies import (
    SharedDependencyVerifier,
    digest,
    maintained_crd_specs,
)

from .test_credential_authority import authority_document

IMAGE = "registry.example/superplane-controller@sha256:" + "a" * 64


def descriptor_document():
    doc = authority_document()
    specs = maintained_crd_specs()
    template = {
        "metadata": {"labels": {"app": "superplane-controller"}},
        "spec": {"containers": [{"name": "controller", "image": IMAGE}]},
    }
    expected = ExpectedPrerequisites(
        "123456789012",
        "vpc-aaa",
        "sg-aaa",
        "sg-bbb",
        "sg-ccc",
        "sg-ddd",
        "vpc-aaa",
        retained_sts_rule_id="sgr-bbb",
    )
    descriptor = {
        "version": 1,
        "observation_protocol": "shared-member-v1",
        "platform_eligible": True,
        "crds": [
            {"name": name, "uid": "crd-" + str(i), "spec_sha256": digest(spec)}
            for i, (name, spec) in enumerate(specs.items())
        ],
        "controller": {
            "namespace": "superplane",
            "namespace_uid": "cp-ns-uid",
            "deployment": "superplane-controller",
            "deployment_uid": "controller-uid",
            "container": "controller",
            "image": IMAGE,
            "template_sha256": digest(template),
        },
        "network": {
            "topology": "same-vpc-private",
            "owner_workspace_id": str(uuid4()),
            "endpoint_rule_id": "sgr-aaa",
            "sts_rule_id": "sgr-bbb",
            "sts_endpoint_id": "vpce-aaa",
            "expected": asdict(expected),
        },
    }
    doc.update(version=2, shared_dependencies=descriptor)
    return doc, specs, template


@pytest.mark.parametrize(
    "change",
    [
        None,
        "v1",
        "missing",
        "unknown",
        "crd",
        "topology",
        "owner",
        "protocol",
        "projection",
    ],
)
def test_dependency_authority_contract_preserves_v1_renewal_and_requires_exact_v2(
    change,
):
    doc, _, _ = descriptor_document()
    if change == "v1":
        doc["version"] = 1
        del doc["shared_dependencies"]
    elif change == "missing":
        del doc["shared_dependencies"]
    elif change == "unknown":
        doc["shared_dependencies"]["trust_current_objects"] = True
    elif change == "crd":
        doc["shared_dependencies"]["crds"].pop()
    elif change == "topology":
        doc["shared_dependencies"]["network"]["topology"] = "arbitrary-peering"
    elif change == "owner":
        doc["shared_dependencies"]["network"]["owner_workspace_id"] = "unknown"
    elif change == "protocol":
        doc["shared_dependencies"]["observation_protocol"] = "future-unreviewed"
    elif change == "projection":
        doc["shared_dependencies"]["controller"]["namespace_uid"] = "other-namespace"
    if change in {None, "v1"}:
        assert Authority.read(str(uuid4()), json.dumps(doc)).document == doc
    else:
        with pytest.raises(BootstrapRefused):
            Authority.read(str(uuid4()), json.dumps(doc))


@pytest.mark.parametrize(
    "drift",
    [
        None,
        "crd-uid",
        "crd-schema",
        "crd-storage",
        "maintained-release",
        "controller-uid",
        "controller-template",
        "controller-stale",
        "controller-unavailable",
        "competing-controller",
        "public-endpoint",
        "wrong-vpc",
        "wrong-group-owner",
        "rule-replaced",
        "sts-replaced",
        "owner-changed",
        "ineligible",
        "v1",
    ],
)
def test_live_dependency_reads_refuse_drift_without_mutating_peers(tmp_path, drift):
    doc, specs, template = descriptor_document()
    if drift == "maintained-release":
        doc["shared_dependencies"]["crds"][0]["spec_sha256"] = "f" * 64
    if drift == "v1":
        doc["version"] = 1
        del doc["shared_dependencies"]
    authority = Authority.read(str(uuid4()), json.dumps(doc))
    workspace_id = str(uuid4())
    target = authority.target(workspace_id)
    original_descriptor = (
        descriptor_document()[0]["shared_dependencies"]
        if drift == "v1"
        else doc["shared_dependencies"]
    )
    owner = original_descriptor["network"]["owner_workspace_id"]
    stores = []

    def grants(management):
        selected = authority.target(workspace_id, management=management)
        ca = tmp_path / ("management.pem" if management else "issuer.pem")
        ca.write_bytes(base64.b64decode(selected.certificate_authority_data))
        objects = {}
        if management:
            objects[("Namespace", None, "superplane")] = {
                "metadata": {"uid": "cp-ns-uid"},
                "status": {"phase": "Active"},
            }
            objects[("Deployment", "superplane", "superplane-controller")] = {
                "metadata": {
                    "namespace": "superplane",
                    "name": "superplane-controller",
                    "uid": "replacement"
                    if drift == "controller-uid"
                    else "controller-uid",
                    "generation": 2,
                },
                "spec": {"replicas": 1, "template": deepcopy(template)},
                "status": {
                    "observedGeneration": 1 if drift == "controller-stale" else 2,
                    "availableReplicas": 0 if drift == "controller-unavailable" else 1,
                    "readyReplicas": 1,
                    "updatedReplicas": 1,
                },
            }
            if drift == "controller-template":
                objects[("Deployment", "superplane", "superplane-controller")]["spec"][
                    "template"
                ]["spec"]["hostNetwork"] = True
        else:
            for i, (name, spec) in enumerate(specs.items()):
                actual = deepcopy(spec)
                actual["conversion"] = {"strategy": "None"}
                actual["preserveUnknownFields"] = False
                if drift == "crd-schema":
                    actual["versions"][0]["served"] = False
                objects[("CustomResourceDefinition", None, name)] = {
                    "metadata": {
                        "uid": "replacement" if drift == "crd-uid" else "crd-" + str(i)
                    },
                    "spec": actual,
                    "status": {
                        "conditions": [{"type": "Established", "status": "True"}],
                        "storedVersions": ["unserved"]
                        if drift == "crd-storage"
                        else [
                            version["name"]
                            for version in spec["versions"]
                            if version["storage"]
                        ],
                    },
                }
        if drift == "competing-controller" and not management:
            objects[("Deployment", "peer", "competitor")] = {
                "metadata": {
                    "namespace": "peer",
                    "name": "competitor",
                    "uid": "peer-uid",
                },
                "spec": {
                    "template": {
                        "spec": {"containers": [{"name": "controller", "image": IMAGE}]}
                    }
                },
            }
        stores.append(objects)

        class Resources:
            def get(self, *, api_version, kind):
                def read(name=None, namespace=None):
                    if name is None:
                        return {
                            "items": [
                                deepcopy(value)
                                for (selected_kind, _, _), value in objects.items()
                                if selected_kind == kind
                            ]
                        }
                    return deepcopy(objects[(kind, namespace, name)])

                return SimpleNamespace(get=read)

        return KubeGrants(
            SimpleNamespace(
                resources=Resources(),
                client=SimpleNamespace(
                    configuration=SimpleNamespace(
                        host=selected.endpoint,
                        ssl_ca_cert=str(ca),
                        verify_ssl=True,
                        assert_hostname=None,
                        tls_server_name=None,
                        proxy=None,
                    )
                ),
            ),
            selected,
        )

    issuer, management = grants(False), grants(True)
    before = deepcopy(stores)
    calls = []

    class Process:
        def run(self, arguments):
            args = list(arguments)
            calls.append(args)
            assert not any(
                word in args
                for word in (
                    "create",
                    "delete",
                    "patch",
                    "authorize-security-group-ingress",
                )
            )
            result = None
            if "describe-cluster" in args:
                name = args[args.index("--name") + 1]
                selected = (
                    doc["target"] if name == "shared" else doc["management_target"]
                )
                result = {
                    "cluster": {
                        "arn": selected["cluster_arn"],
                        "resourcesVpcConfig": {
                            "vpcId": "vpc-bbb" if drift == "wrong-vpc" else "vpc-aaa",
                            "endpointPrivateAccess": True,
                            "endpointPublicAccess": drift == "public-endpoint",
                            "clusterSecurityGroupId": "sg-aaa"
                            if name == "shared"
                            else "sg-bbb",
                            "securityGroupIds": [],
                        },
                    }
                }
            elif "describe-security-groups" in args:
                if "--query" in args:
                    return SimpleNamespace(returncode=0, stdout="vpc-aaa", stderr="")
                result = {
                    "SecurityGroups": [
                        {
                            "GroupId": group,
                            "VpcId": "vpc-aaa",
                            "OwnerId": "000000000000"
                            if drift == "wrong-group-owner"
                            else "123456789012",
                        }
                        for group in ("sg-aaa", "sg-bbb", "sg-ccc", "sg-ddd")
                    ]
                }
            elif "describe-vpc-endpoints" in args:
                result = {
                    "VpcEndpoints": [
                        {
                            "VpcEndpointId": "vpce-bbb"
                            if drift == "sts-replaced"
                            else "vpce-aaa",
                            "VpcId": "vpc-aaa",
                            "State": "available",
                            "VpcEndpointType": "Interface",
                            "PrivateDnsEnabled": True,
                            "ServiceName": "com.amazonaws.us-east-1.sts",
                            "Groups": [{"GroupId": "sg-ddd"}],
                        }
                    ]
                }
            elif "get-caller-identity" in args:
                result = {"Account": "123456789012", "Arn": doc["issuer"]["role_arn"]}
            elif "describe-security-group-rules" in args:
                group = args[args.index("--filters") + 1].split("Values=")[1]
                result = {
                    "SecurityGroupRules": [
                        {
                            "GroupId": group,
                            "OwnerId": "123456789012",
                            "IsEgress": False,
                            "IpProtocol": "tcp",
                            "FromPort": 443,
                            "ToPort": 443,
                            "SecurityGroupRuleId": "sgr-ccc"
                            if drift == "rule-replaced"
                            else "sgr-aaa"
                            if group == "sg-aaa"
                            else "sgr-bbb",
                            "ReferencedGroupInfo": {
                                "GroupId": "sg-bbb" if group == "sg-aaa" else "sg-ccc"
                            },
                            "Tags": [
                                {"Key": "OrgId", "Value": doc["org_id"]},
                                {"Key": "WorkspaceId", "Value": owner},
                            ],
                        }
                    ]
                }
            assert result is not None
            return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

    bridge = SimpleNamespace(
        execute=lambda query, parameters: [
            {
                "workspace_id": str(uuid4()) if drift == "owner-changed" else owner,
                "platform_eligible": drift != "ineligible",
            }
        ]
    )

    def check():
        verifier = SharedDependencyVerifier(
            authority,
            issuer,
            management,
            bridge,
            Process(),
            {"controller_image": IMAGE},
            lambda: None,
        )
        verifier(SimpleNamespace(journal=SimpleNamespace(target=target)))

    if drift:
        with pytest.raises(BootstrapRefused):
            check()
    else:
        check()
        assert any("describe-vpc-endpoints" in call for call in calls)
        assert any("describe-security-group-rules" in call for call in calls)
    assert stores == before
