"""Read-only public EKS bootstrap path, bound to reviewed management NATs."""

import base64
import http.client
import ipaddress
import re
import socket
import ssl
from urllib.parse import urlsplit

from .runtime_config import LifecycleRefused


def public_cidrs(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 8:
        raise LifecycleRefused("public workspace API requires bounded exact NAT /32s")
    result = []
    for value in values:
        try:
            network = ipaddress.ip_network(value, strict=True)
        except (TypeError, ValueError):
            raise LifecycleRefused(
                "public workspace API requires exact NAT /32s"
            ) from None
        if (
            network.version != 4
            or network.prefixlen != 32
            or not network.network_address.is_global
        ):
            raise LifecycleRefused("public workspace API requires global IPv4 NAT /32s")
        result.append(str(network))
    if len(result) != len(set(result)):
        raise LifecycleRefused("public workspace API NAT addresses are duplicated")
    return sorted(result)


def validate_public_management(config):
    recipe = config.get("management_public_access")
    variables = config["workspace_variables"]
    if recipe is None:
        if variables.get("cluster_endpoint_public_access") is True:
            raise LifecycleRefused(
                "public workspace API requires its management NAT recipe"
            )
        return
    if (
        not isinstance(recipe, dict)
        or set(recipe) != {"vpc_id", "nat_gateway_ids"}
        or not isinstance(recipe["vpc_id"], str)
        or not re.fullmatch(r"vpc-[a-f0-9]{8,17}", recipe["vpc_id"])
        or not isinstance(recipe["nat_gateway_ids"], list)
        or not 1 <= len(recipe["nat_gateway_ids"]) <= 8
        or any(
            not isinstance(value, str) or not re.fullmatch(r"nat-[a-f0-9]{8,17}", value)
            for value in recipe["nat_gateway_ids"]
        )
        or len(set(recipe["nat_gateway_ids"])) != len(recipe["nat_gateway_ids"])
        or variables.get("cluster_endpoint_public_access") is not True
        or variables.get("networking_mode", "owned") != "owned"
    ):
        raise LifecycleRefused(
            "public workspace API requires an exact owned-network NAT recipe"
        )
    public_cidrs(variables.get("cluster_endpoint_public_access_cidrs"))


def probe_public_path(endpoint, certificate):
    """No bearer tokens, proxy environment, redirects or ambient kubeconfig."""
    origin = urlsplit(endpoint)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.port not in {None, 443}
        or origin.username
        or origin.password
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
        or not origin.hostname.endswith((".eks.amazonaws.com", ".api.aws"))
    ):
        raise LifecycleRefused("verified workspace EKS endpoint is invalid")
    try:
        ca = base64.b64decode(certificate, validate=True).decode("ascii")
        context = ssl.create_default_context(cadata=ca)
        # Resolving public addresses here prevents accidental use of a private
        # route from being recorded as proof of the selected public path.
        addresses = socket.getaddrinfo(
            origin.hostname, 443, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise LifecycleRefused(
                "public workspace endpoint resolved outside public IPv4"
            )
        with socket.create_connection(addresses[0][4], timeout=10) as transport:
            with context.wrap_socket(transport, server_hostname=origin.hostname):
                pass
        connection = http.client.HTTPSConnection(
            "checkip.amazonaws.com", timeout=10, context=ssl.create_default_context()
        )
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            body = response.read(65)
            if response.status != 200 or len(body) > 64:
                raise LifecycleRefused(
                    "management public source identity was not observed"
                )
            address = ipaddress.ip_address(body.decode("ascii").strip())
            if address.version != 4 or not address.is_global:
                raise LifecycleRefused("management source identity is not public IPv4")
            return str(address)
        finally:
            connection.close()
    except (OSError, ValueError, UnicodeError, http.client.HTTPException):
        raise LifecycleRefused(
            "public workspace TLS/source reachability proof failed"
        ) from None


def observe_public_api(config, outputs, read, verify, *, probe=None):
    validate_public_management(config)
    recipe = config.get("management_public_access")
    if recipe is None:
        raise LifecycleRefused("public workspace path is not configured")
    expected = public_cidrs(
        config["workspace_variables"]["cluster_endpoint_public_access_cidrs"]
    )
    if (
        outputs.get("cluster_endpoint_public_access") is not True
        or public_cidrs(outputs.get("cluster_endpoint_public_access_cidrs")) != expected
    ):
        raise LifecycleRefused(
            "reviewed workspace outputs differ from the public API recipe"
        )
    verify()
    identity = read("sts", "get_caller_identity")
    cluster = read("eks", "describe_cluster", name=outputs["cluster_name"])["cluster"]
    vpc = cluster.get("resourcesVpcConfig", {})
    if (
        identity.get("Account") != outputs["account_id"]
        or cluster.get("arn") != outputs["cluster_arn"]
        or cluster.get("name") != outputs["cluster_name"]
        or cluster.get("status") != "ACTIVE"
        or cluster.get("accessConfig", {}).get("authenticationMode") != "API"
        or cluster.get("endpoint") != outputs["cluster_endpoint"]
        or cluster.get("certificateAuthority", {}).get("data")
        != outputs["cluster_certificate_authority_data"]
        or vpc.get("vpcId") != outputs["vpc_id"]
        or vpc.get("endpointPublicAccess") is not True
        or vpc.get("endpointPrivateAccess") is not True
        or public_cidrs(vpc.get("publicAccessCidrs")) != expected
    ):
        raise LifecycleRefused(
            "live workspace public endpoint differs from its reviewed identity"
        )
    response = read(
        "ec2", "describe_nat_gateways", NatGatewayIds=recipe["nat_gateway_ids"]
    )
    gateways = response.get("NatGateways", [])
    if (
        response.get("NextToken")
        or len(gateways) != len(recipe["nat_gateway_ids"])
        or {item.get("NatGatewayId") for item in gateways}
        != set(recipe["nat_gateway_ids"])
    ):
        raise LifecycleRefused("management NAT inventory is incomplete")
    addresses = []
    for gateway in gateways:
        if (
            gateway.get("VpcId") != recipe["vpc_id"]
            or gateway.get("State") != "available"
            or gateway.get("ConnectivityType") != "public"
        ):
            raise LifecycleRefused("management NAT identity or state differs")
        for address in gateway.get("NatGatewayAddresses", []):
            if address.get("Status", "succeeded") != "succeeded" or not address.get(
                "AllocationId"
            ):
                raise LifecycleRefused(
                    "management NAT public allocation is unavailable"
                )
            addresses.append(str(address.get("PublicIp")) + "/32")
    if public_cidrs(addresses) != expected:
        raise LifecycleRefused(
            "live management NAT addresses differ from the reviewed allowlist"
        )
    verify()
    source = (probe or probe_public_path)(
        outputs["cluster_endpoint"], outputs["cluster_certificate_authority_data"]
    )
    verify()
    if source + "/32" not in expected:
        raise LifecycleRefused(
            "worker public source is outside the reviewed management NATs"
        )
    return {
        "cluster_arn": outputs["cluster_arn"],
        "endpoint": outputs["cluster_endpoint"],
        "public_access_cidrs": expected,
        "source_address": source,
        "management_vpc_id": recipe["vpc_id"],
        "nat_gateway_ids": sorted(recipe["nat_gateway_ids"]),
        "tls_verified": True,
        "created": False,
    }
