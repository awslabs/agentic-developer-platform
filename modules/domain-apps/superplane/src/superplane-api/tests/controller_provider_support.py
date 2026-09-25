"""Simulated provider I/O for the governed API through real worker/RPC tests.

These doubles implement only AWS/SkyPilot/Kubernetes I/O. They never grant paid
execution, invent admission rows, or calculate allocation release decisions.
"""

import base64
import json
from uuid import uuid4

import httpx
from superplane_executor.workspace import Workspace


class Cloud:
    def __init__(self, data):
        self.data = data
        self.exists = False
        self.ever_created = False
        self.leaked_volume = False
        self.launches = 0
        self.sky_account = data["provider_account_id"]
        self.lose_launch_response = False
        # A kill AFTER the durable handle is journalled but BEFORE the launch is
        # observed: the state scoped recovery exists to resolve.
        self.lose_status_response = False
        # #5925: which approved region actually has capacity, for a version-4
        # (multi-region) plan. `None` names every region as capacity-exhausted.
        # Ignored entirely for version 1-3 plans (still single-region).
        self.capacity_region = (
            data["regions"][0]["region"] if "regions" in data else None
        )
        self.launched_region = None
        self._client_region = None
        self.instance = {
            "InstanceId": "i-0123456789abcdef0",
            "ImageId": data.get("image_id", "ami-0000000000000000"),
            # The simulated SkyPilot transport chooses this machine for GPU
            # requirement plans; the producer itself no longer picks one.
            "InstanceType": data.get("instance_type", "g6.xlarge"),
            "State": {"Name": "running"},
            "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-0123456789abcdef0"}}],
            "NetworkInterfaces": [{"NetworkInterfaceId": "eni-0123456789abcdef0"}],
        }

    def client(self, name, region_name=None, **kwargs):
        self._client_region = region_name
        return self

    @property
    def meta(self):
        cloud = self

        class Meta:
            region_name = cloud._client_region

        return Meta()

    def get_caller_identity(self):
        return {
            "Account": self.data["provider_account_id"],
            "Arn": f"arn:aws:sts::{self.data['provider_account_id']}:assumed-role/approved/provider",
        }

    def describe_cluster(self, **kwargs):
        d = self.data
        return {
            "cluster": {
                "name": "workspace",
                "arn": d["cluster_arn"],
                "endpoint": d["endpoint"],
                "status": "ACTIVE",
                "certificateAuthority": {"data": d["certificate_authority"]},
                "kubernetesNetworkConfig": {"serviceIpv4Cidr": d["service_cidr"]},
                "resourcesVpcConfig": {
                    "vpcId": "vpc-0123456789abcdef0"
                    if "regions" in d
                    else "vpc-approved"
                },
            }
        }

    def describe_vpcs(self, **kwargs):
        return {
            "Vpcs": [
                {
                    "VpcId": "vpc-0123456789abcdef0"
                    if "regions" in self.data
                    else "vpc-approved"
                }
            ]
        }

    def describe_security_groups(self, **kwargs):
        return {
            "SecurityGroups": [
                {
                    "GroupId": "sg-0123456789abcdef0"
                    if "regions" in self.data
                    else "sg-approved",
                    "VpcId": "vpc-0123456789abcdef0",
                    "OwnerId": self.data["provider_account_id"],
                }
            ]
        }

    def describe_subnets(self, SubnetIds):
        return {
            "Subnets": [
                {
                    "SubnetId": s,
                    "VpcId": "vpc-0123456789abcdef0",
                    "OwnerId": self.data["provider_account_id"],
                }
                for s in SubnetIds
            ]
        }

    def describe_images(self, ImageIds):
        return {"Images": [{"ImageId": i, "State": "available"} for i in ImageIds]}

    def get_instance_profile(self, **kwargs):
        return {
            "InstanceProfile": {
                "Arn": f"arn:aws:iam::{self.data['provider_account_id']}:instance-profile/approved",
                "Roles": [
                    {
                        "Arn": f"arn:aws:iam::{self.data['provider_account_id']}:role/node"
                    }
                ],
            }
        }

    def describe_access_entry(self, **kwargs):
        return {"accessEntry": {"type": "EC2_LINUX"}}

    def get_paginator(self, method):
        cloud = self

        class Paginator:
            def paginate(self, **kwargs):
                if method == "describe_instances":
                    include_old = not any(
                        f["Name"] == "instance-state-name"
                        for f in kwargs.get("Filters", [])
                    )
                    return [
                        cloud.describe_instances(InstanceIds=["known"])
                        if include_old or cloud.exists
                        else {"Reservations": []}
                    ]
                return [getattr(cloud, method)(**kwargs)]

        return Paginator()

    def _here(self):
        """Whether the requested resource belongs in the client's own region.

        Single-region (version 1-3) plans never set `launched_region`, so this
        is always True for them -- unchanged existing behavior. A version-4
        (multi-region) plan only reports its instance/attachments present in
        the one region SkyPilot actually used; every other approved region
        legitimately reports empty, the way real capacity absence looks.
        """
        return (
            self.launched_region is None or self._client_region == self.launched_region
        )

    def describe_instances(self, **kwargs):
        if not self.ever_created or not self._here():
            return {"Reservations": []}
        return {
            "Reservations": [
                {
                    "Instances": [
                        dict(
                            self.instance,
                            State={"Name": "running" if self.exists else "terminated"},
                        )
                    ]
                }
            ]
        }

    def describe_volumes(self, **kwargs):
        return {
            "Volumes": [{"VolumeId": "vol-0123456789abcdef0", "State": "in-use"}]
            if (self.exists or self.leaked_volume) and self._here()
            else []
        }

    def describe_network_interfaces(self, **kwargs):
        return {
            "NetworkInterfaces": [
                {"NetworkInterfaceId": "eni-0123456789abcdef0", "Status": "in-use"}
            ]
            if self.exists and self._here()
            else []
        }

    def describe_addresses(self, **kwargs):
        return {"Addresses": []}


class Kubernetes(Workspace):
    def __init__(self, cloud):
        self.cloud = cloud
        self.stored = {}
        self.requests = []

    async def request(
        self, operation, target, method, path, *, body=None, headers=None
    ):
        self.requests.append((method, path))
        if path == "/api/v1/namespaces/" + self.cloud.data["namespace"]:
            return httpx.Response(200, json={"status": {"phase": "Active"}})
        if "/superplane.ai/" in path:
            return httpx.Response(200, json={"items": []})
        if path.startswith("/api/v1/nodes?"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "spec": {
                                "providerID": "aws:///us-east-1a/i-0123456789abcdef0"
                            },
                            "status": {
                                "conditions": [{"type": "Ready", "status": "True"}]
                            },
                        }
                    ]
                    if self.cloud.exists
                    else []
                },
            )
        if "/pods?" in path:
            return httpx.Response(200, json={"items": []})
        if "/secrets/" in path:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "token": base64.b64encode(
                            b"test-only-serving-token-" + b"a" * 32
                        ).decode()
                    }
                },
            )
        if path.endswith("/proxy/healthz"):
            return httpx.Response(200 if headers else 401)
        if method == "POST":
            obj = json.loads(json.dumps(body))
            obj["metadata"]["uid"] = str(uuid4())
            obj["status"] = {
                "succeeded": 1,
                "availableReplicas": 1,
                "observedGeneration": 1,
            }
            obj["metadata"]["generation"] = 1
            key = path + "/" + obj["metadata"]["name"]
            if key in self.stored:
                return httpx.Response(409)
            self.stored[key] = obj
            return httpx.Response(201, json=obj)
        if method == "DELETE":
            obj = self.stored.get(path)
            if obj and body["preconditions"]["uid"] != obj["metadata"]["uid"]:
                return httpx.Response(409)
            self.stored.pop(path, None)
            return httpx.Response(200, json={})
        obj = self.stored.get(path)
        return httpx.Response(200, json=obj) if obj else httpx.Response(404)
