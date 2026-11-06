"""Finite read-only SDK facade for trusted API census; never returned to workers."""

import json
from datetime import datetime
import threading
import time

from harness_jobs.identity import OperationRefused


INVENTORY_READS = {
    ("sts", "get_caller_identity"): "sts:GetCallerIdentity",
    ("resourcegroupstaggingapi", "get_resources"): "tag:GetResources",
    ("iam", "get_role"): "iam:GetRole",
    ("iam", "get_role_policy"): "iam:GetRolePolicy",
    ("iam", "get_open_id_connect_provider"): "iam:GetOpenIDConnectProvider",
    ("iam", "list_attached_role_policies"): "iam:ListAttachedRolePolicies",
    ("kms", "describe_key"): "kms:DescribeKey",
    ("kms", "list_aliases"): "kms:ListAliases",
    ("logs", "describe_log_groups"): "logs:DescribeLogGroups",
    **{
        ("ec2", method): action
        for method, action in (
            ("describe_instances", "ec2:DescribeInstances"),
            ("describe_network_interfaces", "ec2:DescribeNetworkInterfaces"),
            ("describe_volumes", "ec2:DescribeVolumes"),
            ("describe_addresses", "ec2:DescribeAddresses"),
            ("describe_vpcs", "ec2:DescribeVpcs"),
            ("describe_subnets", "ec2:DescribeSubnets"),
            ("describe_internet_gateways", "ec2:DescribeInternetGateways"),
            ("describe_nat_gateways", "ec2:DescribeNatGateways"),
            ("describe_route_tables", "ec2:DescribeRouteTables"),
            ("describe_launch_templates", "ec2:DescribeLaunchTemplates"),
        )
    },
}


class ReadSession:
    def __init__(self, session, *, region, config, reads, limit=512, seconds=18):
        self._session, self._region, self._config = session, region, config
        self._reads, self._remaining = reads, limit
        self._deadline = time.monotonic() + seconds
        self._clients, self._lock = {}, threading.Lock()

    def client(self, service, *, region_name):
        if region_name != self._region or not any(s == service for s, _ in self._reads):
            raise OperationRefused("recovery census provider scope changed")
        with self._lock:
            if service not in self._clients:
                self._clients[service] = self._session.client(
                    service, region_name=region_name, config=self._config
                )
            client = self._clients[service]
        owner = self

        class Client:
            def __getattr__(self, method):
                if (service, method) not in owner._reads:
                    raise OperationRefused("recovery census only permits finite reads")

                def read(**arguments):
                    with owner._lock:
                        if owner._remaining <= 0 or time.monotonic() >= owner._deadline:
                            raise OperationRefused(
                                "recovery census read bound exceeded"
                            )
                        owner._remaining -= 1
                    result = getattr(client, method)(**arguments)
                    if time.monotonic() >= owner._deadline:
                        raise OperationRefused("recovery census read deadline exceeded")

                    def encode(value):
                        if isinstance(value, datetime):
                            return value.isoformat()
                        raise TypeError("unsupported recovery response")

                    if (
                        not isinstance(result, dict)
                        or len(
                            json.dumps(result, default=encode, allow_nan=False).encode()
                        )
                        > 4 * 2**20
                    ):
                        raise OperationRefused(
                            "recovery census response exceeded its bound"
                        )
                    return result

                return read

            def get_paginator(self, method):
                # Botocore's paginator must invoke the guarded method for EVERY
                # page, including automatic continuation-token requests.
                guarded = self.__getattr__(method)
                paginator = client.get_paginator(method)
                paginator._method = guarded
                return paginator

        return Client()

    def close(self):
        for client in self._clients.values():
            client.close()
