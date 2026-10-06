"""Public bootstrap proves real provider/NAT/TLS identity without ingress mutation."""

from copy import deepcopy

import pytest

from workspace_provisioning.public_network import (
    observe_public_api,
    validate_public_management,
)
from workspace_provisioning.runtime_config import LifecycleRefused


def fixture():
    cidrs = ["52.22.137.37/32"]
    config = {
        "workspace_variables": {
            "networking_mode": "owned",
            "cluster_endpoint_public_access": True,
            "cluster_endpoint_public_access_cidrs": cidrs,
        },
        "management_public_access": {
            "vpc_id": "vpc-0123456789abcdef0",
            "nat_gateway_ids": ["nat-0123456789abcdef0"],
        },
    }
    outputs = {
        "account_id": "111111111111",
        "vpc_id": "vpc-22222222222222222",
        "cluster_name": "workspace",
        "cluster_arn": "arn:aws:eks:us-east-1:111111111111:cluster/workspace",
        "cluster_endpoint": "https://workspace.eks.amazonaws.com",
        "cluster_certificate_authority_data": "verified-ca",
        "cluster_endpoint_public_access": True,
        "cluster_endpoint_public_access_cidrs": cidrs,
    }
    cluster = {
        "accessConfig": {"authenticationMode": "API"},
        "arn": outputs["cluster_arn"],
        "name": outputs["cluster_name"],
        "status": "ACTIVE",
        "endpoint": outputs["cluster_endpoint"],
        "certificateAuthority": {"data": "verified-ca"},
        "resourcesVpcConfig": {
            "vpcId": outputs["vpc_id"],
            "endpointPublicAccess": True,
            "endpointPrivateAccess": True,
            "publicAccessCidrs": list(cidrs),
        },
    }
    gateway = {
        "NatGatewayId": config["management_public_access"]["nat_gateway_ids"][0],
        "VpcId": config["management_public_access"]["vpc_id"],
        "State": "available",
        "ConnectivityType": "public",
        "NatGatewayAddresses": [
            {
                "PublicIp": "52.22.137.37",
                "AllocationId": "eipalloc-0123456789abcdef0",
                "Status": "succeeded",
            }
        ],
    }
    calls = []

    def read(service, method, **arguments):
        calls.append((service, method, arguments))
        if (service, method) == ("sts", "get_caller_identity"):
            return {"Account": outputs["account_id"]}
        if (service, method) == ("eks", "describe_cluster"):
            assert arguments == {"name": "workspace"}
            return {"cluster": deepcopy(cluster)}
        assert (service, method) == ("ec2", "describe_nat_gateways")
        assert arguments == {"NatGatewayIds": [gateway["NatGatewayId"]]}
        return {"NatGateways": [deepcopy(gateway)]}

    def probe(endpoint, certificate):
        assert (endpoint, certificate) == (outputs["cluster_endpoint"], "verified-ca")
        calls.append("tls/source")
        return "52.22.137.37"

    return config, outputs, cluster, gateway, read, probe, calls


def test_public_path_records_exact_endpoint_and_no_removable_rule():
    config, outputs, _, _, read, probe, calls = fixture()
    result = observe_public_api(
        config, outputs, read, lambda: calls.append("authority"), probe=probe
    )
    assert result["cluster_arn"] == outputs["cluster_arn"]
    assert result["created"] is False and result["tls_verified"] is True
    assert result["public_access_cidrs"] == ["52.22.137.37/32"]
    assert calls[-1] == "authority"
    assert "rule_id" not in result


@pytest.mark.parametrize(
    "fault",
    [
        "widened",
        "replaced-ca",
        "wrong-vpc",
        "private",
        "missing-eip",
        "changed-eip",
        "pending-nat",
        "foreign-nat",
        "wrong-source",
        "unreviewed-output",
        "revoked",
    ],
)
def test_changed_public_identity_refuses_without_network_effects(fault):
    config, outputs, cluster, gateway, read, probe, calls = fixture()
    if fault == "widened":
        cluster["resourcesVpcConfig"]["publicAccessCidrs"] = ["0.0.0.0/0"]
    elif fault == "replaced-ca":
        cluster["certificateAuthority"]["data"] = "other-ca"
    elif fault == "wrong-vpc":
        cluster["resourcesVpcConfig"]["vpcId"] = "another"
    elif fault == "private":
        cluster["resourcesVpcConfig"]["endpointPublicAccess"] = False
    elif fault == "missing-eip":
        gateway["NatGatewayAddresses"] = []
    elif fault == "changed-eip":
        gateway["NatGatewayAddresses"][0]["PublicIp"] = "8.8.8.8"
    elif fault == "pending-nat":
        gateway["State"] = "pending"
    elif fault == "foreign-nat":
        gateway["VpcId"] = "another"
    elif fault == "wrong-source":

        def probe(*args):
            return "8.8.8.8"
    elif fault == "unreviewed-output":
        outputs["cluster_endpoint_public_access_cidrs"] = ["8.8.8.8/32"]

    def verify():
        if fault == "revoked":
            raise LifecycleRefused("revoked")

    with pytest.raises(LifecycleRefused):
        observe_public_api(config, outputs, read, verify, probe=probe)


def test_private_mode_cannot_silently_select_public_transport():
    validate_public_management({"workspace_variables": {}})
    with pytest.raises(LifecycleRefused, match="NAT recipe"):
        validate_public_management(
            {"workspace_variables": {"cluster_endpoint_public_access": True}}
        )
