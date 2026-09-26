"""Compose existing network SDK fixtures with the real paid API worker fixture."""

import importlib.util
from pathlib import Path
from urllib.parse import urlsplit


def support():
    spec = importlib.util.spec_from_file_location(
        "paid_controller_network_fixture",
        Path(__file__).resolve().parents[3] / "executor/tests/network_support.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure(profile):
    native = support()
    for name in (
        "instance_type",
        "region",
        "image_id",
        "vpc_name",
        "security_group",
        "instance_profile",
    ):
        profile.pop(name)
    profile["cluster_arn"] = profile["cluster_arn"].replace(
        ":us-west-2:", ":us-east-1:"
    )
    profile.update(accelerators=["L4:1"], max_gpus_per_node=1, cpus=4, memory_gb=32)
    profile["network"] = native.policy(profile["cluster_id"])
    profile["regions"] = [
        {
            "region": region,
            "image_id": "ami-0123456789abcdef0",
            "vpc_name": "approved",
            "security_group": "approved",
            "instance_profile": "approved-nodes",
            **{
                key: native.side(index)[key]
                for key in ("vpc_id", "security_group_id", "subnet_ids")
            },
        }
        for region, index in ((native.REMOTE, 2), (native.HOME, 1))
    ]


async def attach(pool, cloud, workload):
    native = support()
    native.ACCOUNT = cloud.data["provider_account_id"]
    aws = native.AWS()
    await native.schema(pool)
    async with pool.acquire() as c:
        await c.execute("""CREATE TABLE cluster_memberships(workspace_id uuid,org_id uuid,cluster_id uuid,generation text,namespace text,state text);
            INSERT INTO cluster_memberships SELECT id,org_id,cluster_id,repeat('a',64),namespace_name,'active' FROM workspaces;""")
    original = aws.respond

    def respond(region, service, name, args):
        result = original(region, service, name, args)
        if name == "describe_cluster":
            result["cluster"].update(cloud.describe_cluster()["cluster"])
            result["cluster"]["resourcesVpcConfig"] = {
                "vpcId": native.side(1)["vpc_id"],
                "endpointPublicAccess": False,
                "endpointPrivateAccess": True,
                "clusterSecurityGroupId": native.side(1)["security_group_id"],
            }
        elif name == "get_resolver_rule":
            result["ResolverRule"]["DomainName"] = (
                urlsplit(cloud.data["endpoint"]).hostname + "."
            )
        return result

    aws.respond = respond
    original_client = cloud.client

    def client(service, region_name=None, **kwargs):
        current = original_client(service, region_name=region_name, **kwargs)
        sdk = aws.client(service, region_name=region_name)

        class Client:
            def __getattr__(self, name):
                if name in {
                    "get_caller_identity",
                    "get_instance_profile",
                    "describe_access_entry",
                    "describe_images",
                    "describe_instances",
                    "describe_volumes",
                    "describe_network_interfaces",
                    "describe_addresses",
                    "get_paginator",
                    "meta",
                }:
                    return getattr(current, name)
                return getattr(sdk, name)

        return Client()

    cloud.client = client
    cloud.network = aws
