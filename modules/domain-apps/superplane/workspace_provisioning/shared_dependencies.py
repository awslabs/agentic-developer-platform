"""Versioned installation pins and read-only shared bootstrap dependency proof."""

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID

from superplane_bootstrap.components import CONTROLLER_IMAGE_MARKER, WORKSPACE_CRDS
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import _payload
from superplane_bootstrap.prerequisites import (
    ExpectedPrerequisites,
    verify_network_prerequisites,
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise BootstrapRefused("shared dependency descriptor fields differ")


def _uid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise BootstrapRefused("shared dependency UID is invalid")


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise BootstrapRefused("shared dependency digest is invalid")


def validate_descriptor(value, authority):
    """Closed source contract; validation never reads live objects to fill pins."""
    _fields(
        value, "version observation_protocol platform_eligible crds controller network"
    )
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["observation_protocol"] != "shared-member-v1"
        or type(value["platform_eligible"]) is not bool
        or (
            authority["target"]["cluster_arn"]
            == authority["management_target"]["cluster_arn"]
            and value["platform_eligible"] is not True
        )
    ):
        raise BootstrapRefused(
            "shared dependency version or platform policy is unsupported"
        )
    crds = value["crds"]
    if not isinstance(crds, list) or len(crds) != len(WORKSPACE_CRDS):
        raise BootstrapRefused("shared dependency CRD inventory is incomplete")
    for item in crds:
        _fields(item, "name uid spec_sha256")
        _uid(item["uid"])
        _hash(item["spec_sha256"])
    if {item["name"] for item in crds} != set(WORKSPACE_CRDS):
        raise BootstrapRefused(
            "shared dependency CRD inventory differs from the maintained release"
        )
    controller = value["controller"]
    _fields(
        controller,
        "namespace namespace_uid deployment deployment_uid container image template_sha256",
    )
    for name in ("namespace", "deployment", "container"):
        if not isinstance(controller[name], str) or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", controller[name]
        ):
            raise BootstrapRefused("shared controller name is invalid")
    _uid(controller["namespace_uid"])
    _uid(controller["deployment_uid"])
    _hash(controller["template_sha256"])
    if (
        not isinstance(controller["image"], str)
        or CONTROLLER_IMAGE_MARKER not in controller["image"]
        or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", controller["image"])
    ):
        raise BootstrapRefused(
            "shared controller image is not a pinned maintained controller"
        )
    if (controller["namespace"], controller["namespace_uid"]) != (
        authority["projection"]["namespace"],
        authority["projection"]["namespace_uid"],
    ):
        raise BootstrapRefused(
            "shared controller and projection namespace identities differ"
        )
    network = value["network"]
    _fields(
        network,
        "topology owner_workspace_id endpoint_rule_id sts_rule_id sts_endpoint_id expected",
    )
    if (
        network["topology"] != "same-vpc-private"
        or str(UUID(network["owner_workspace_id"])) != network["owner_workspace_id"]
    ):
        raise BootstrapRefused("shared network topology is unsupported")
    for name in ("endpoint_rule_id", "sts_rule_id"):
        if not isinstance(network[name], str) or not re.fullmatch(
            r"sgr-[a-f0-9]+", network[name]
        ):
            raise BootstrapRefused("shared network requires exact rule IDs")
    if not isinstance(network["sts_endpoint_id"], str) or not re.fullmatch(
        r"vpce-[a-f0-9]+", network["sts_endpoint_id"]
    ):
        raise BootstrapRefused("shared network requires an exact STS endpoint ID")
    _fields(network["expected"], " ".join(ExpectedPrerequisites.__dataclass_fields__))
    expected = ExpectedPrerequisites(**network["expected"])
    if (
        expected.account_id != authority["target"]["account_id"]
        or expected.account_id != authority["management_target"]["account_id"]
        or authority["target"]["region"] != authority["management_target"]["region"]
        or expected.vpc_id != expected.sts_endpoint_vpc_id
        or expected.protocol != "tcp"
        or expected.api_server_port != 443
        or expected.retained_sts_rule_id != network["sts_rule_id"]
    ):
        raise BootstrapRefused("shared private network compatibility differs")
    for name, identifier in asdict(expected).items():
        if name.endswith("security_group_id") and not re.fullmatch(
            r"sg-[a-f0-9]+", identifier
        ):
            raise BootstrapRefused("shared network group identity is invalid")
        if name.endswith("vpc_id") and not re.fullmatch(r"vpc-[a-f0-9]+", identifier):
            raise BootstrapRefused("shared network VPC identity is invalid")
    return value


def maintained_crd_specs():
    import yaml

    source = Path(__file__).with_name("_data") / "crds.yaml"
    if not source.is_file():
        source = (
            Path(__file__).resolve().parents[1]
            / "src/superplane-controller/deploy/crds.yaml"
        )
    if not source.is_file():
        raise BootstrapRefused("maintained shared CRD release is missing")
    documents = list(yaml.safe_load_all(source.read_text()))
    specs = {
        item["metadata"]["name"]: item["spec"]
        for item in documents
        if isinstance(item, dict) and item.get("kind") == "CustomResourceDefinition"
    }
    if set(specs) != set(WORKSPACE_CRDS):
        raise BootstrapRefused("maintained shared CRD release is incomplete")
    return specs


class SharedDependencyVerifier:
    def __init__(
        self, installation, issuer, management, bridge, process, config, verify
    ):
        from superplane_bootstrap.adapters import AwsPrerequisiteAccess

        document = installation.document
        if document.get("version") != 2:
            raise BootstrapRefused(
                "shared bootstrap requires a version-2 dependency descriptor"
            )
        self.descriptor = validate_descriptor(document["shared_dependencies"], document)
        self.installation, self.issuer, self.management = (
            installation,
            issuer,
            management,
        )
        self.bridge, self.process, self.verify = bridge, process, verify
        self.network_access = AwsPrerequisiteAccess(process, issuer.target.region)
        self.specs = maintained_crd_specs()
        if self.descriptor["controller"]["image"] != config["controller_image"] or any(
            item["spec_sha256"] != digest(self.specs[item["name"]])
            for item in self.descriptor["crds"]
        ):
            raise BootstrapRefused(
                "installed shared dependencies differ from maintained worker release"
            )

    def _read(self, grants, kind, name, namespace=None):
        self.verify()
        body = {
            "apiVersion": "apps/v1"
            if kind == "Deployment"
            else "apiextensions.k8s.io/v1"
            if kind == "CustomResourceDefinition"
            else "v1",
            "kind": kind,
            "metadata": {"name": name},
        }
        if namespace:
            body["metadata"]["namespace"] = namespace
        value = grants._get({"cluster_arn": grants.target.cluster_arn, "body": body})
        self.verify()
        if value is None or value.get("metadata", {}).get("deletionTimestamp"):
            raise BootstrapRefused(
                "installed shared dependency is missing or terminating"
            )
        return value

    def _controllers(self, grants):
        self.verify()
        grants._verify_transport()
        result = _payload(
            grants.client.resources.get(api_version="apps/v1", kind="Deployment").get()
        )
        self.verify()
        items = result.get("items")
        if (
            not isinstance(items, list)
            or len(items) > 4096
            or result.get("metadata", {}).get("continue")
        ):
            raise BootstrapRefused("shared controller inventory is incomplete")
        found = []
        for item in items:
            for container in (
                item.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("containers", [])
            ):
                if CONTROLLER_IMAGE_MARKER in container.get("image", ""):
                    meta = item.get("metadata", {})
                    found.append(
                        (
                            meta.get("namespace"),
                            meta.get("name"),
                            meta.get("uid"),
                            container.get("name"),
                        )
                    )
        return found

    def __call__(self, authority):
        from superplane_bootstrap.adapters import AwsObserver

        self.verify()
        target = authority.journal.target
        document, descriptor = self.installation.document, self.descriptor
        network = descriptor["network"]
        expected = ExpectedPrerequisites(**network["expected"])
        rows = self.bridge.execute(
            "SELECT workspace_id::text,platform_eligible FROM clusters WHERE id::text=:cluster AND org_id::text=:org "
            "AND eks_cluster_arn=:arn AND endpoint=:endpoint AND sharing_enabled AND status IN ('Ready','Active')",
            {
                "cluster": document["cluster_id"],
                "org": target.org_id,
                "arn": target.cluster_arn,
                "endpoint": target.endpoint,
            },
        )
        if (
            len(rows) != 1
            or rows[0]["workspace_id"] != network["owner_workspace_id"]
            or rows[0]["platform_eligible"] != descriptor["platform_eligible"]
        ):
            raise BootstrapRefused(
                "shared cluster owner or platform eligibility changed"
            )
        # Current descriptor supports private same-VPC clusters only. Read both
        # actual EKS VPC identities; group reachability cannot establish peering.
        for key in ("target", "management_target"):
            selected = document[key]
            self.verify()
            response = self.process.run(
                (
                    "aws",
                    "--region",
                    selected["region"],
                    "eks",
                    "describe-cluster",
                    "--name",
                    selected["cluster_name"],
                    "--output",
                    "json",
                )
            )
            self.verify()
            if response.returncode != 0:
                raise BootstrapRefused("shared network cluster observation failed")
            cluster = json.loads(response.stdout)["cluster"]
            vpc = cluster.get("resourcesVpcConfig", {})
            if (
                cluster.get("arn") != selected["cluster_arn"]
                or vpc.get("vpcId") != expected.vpc_id
                or vpc.get("endpointPrivateAccess") is not True
                or vpc.get("endpointPublicAccess") is not False
            ):
                raise BootstrapRefused(
                    "shared cluster private network topology changed"
                )
            attached = {
                vpc.get("clusterSecurityGroupId"),
                *vpc.get("securityGroupIds", []),
            }
            group = (
                expected.cluster_security_group_id
                if key == "target"
                else expected.management_security_group_id
            )
            if group not in attached:
                raise BootstrapRefused(
                    "shared API security group is not attached to its cluster"
                )
        group_ids = sorted(
            {
                value
                for name, value in asdict(expected).items()
                if name.endswith("security_group_id")
            }
        )
        self.verify()
        result = self.process.run(
            (
                "aws",
                "--region",
                target.region,
                "ec2",
                "describe-security-groups",
                "--group-ids",
                *group_ids,
                "--output",
                "json",
            )
        )
        self.verify()
        if result.returncode != 0:
            raise BootstrapRefused("shared security group inventory is unavailable")
        groups = json.loads(result.stdout).get("SecurityGroups", [])
        if (
            len(groups) != len(group_ids)
            or {group.get("GroupId") for group in groups} != set(group_ids)
            or any(
                group.get("VpcId") != expected.vpc_id
                or group.get("OwnerId") != target.account_id
                for group in groups
            )
        ):
            raise BootstrapRefused("shared security group owner or VPC changed")
        self.verify()
        result = self.process.run(
            (
                "aws",
                "--region",
                target.region,
                "ec2",
                "describe-vpc-endpoints",
                "--vpc-endpoint-ids",
                network["sts_endpoint_id"],
                "--output",
                "json",
            )
        )
        self.verify()
        if result.returncode != 0:
            raise BootstrapRefused("shared STS endpoint observation is unavailable")
        endpoints = json.loads(result.stdout).get("VpcEndpoints", [])
        if (
            len(endpoints) != 1
            or any(
                endpoints[0].get(key) != value
                for key, value in {
                    "VpcEndpointId": network["sts_endpoint_id"],
                    "VpcId": expected.vpc_id,
                    "State": "available",
                    "VpcEndpointType": "Interface",
                    "PrivateDnsEnabled": True,
                    "ServiceName": f"com.amazonaws.{target.region}.sts",
                }.items()
            )
            or expected.sts_endpoint_security_group_id
            not in {group.get("GroupId") for group in endpoints[0].get("Groups", [])}
        ):
            raise BootstrapRefused(
                "shared STS endpoint identity or private service path changed"
            )
        provider = AwsObserver(self.process, target.region).provider_identity()

        class ExistingNetwork:
            def security_group_rule(inner, *args):
                # This adapter is read-only; observed rules predate this member.
                return {
                    **self.network_access.security_group_rule(*args),
                    "created_by_bootstrap": False,
                }

        inventory = verify_network_prerequisites(
            access=ExistingNetwork(),
            target=replace(target, workspace_id=network["owner_workspace_id"]),
            expected=expected,
            provider_account_id=provider.account_id,
        )
        if [item.identifier for item in inventory] != [
            network["endpoint_rule_id"],
            network["sts_rule_id"],
        ]:
            raise BootstrapRefused("shared network rule incarnation changed")
        for item in descriptor["crds"]:
            actual = self._read(self.issuer, "CustomResourceDefinition", item["name"])
            spec = dict(actual.get("spec", {}))
            desired = self.specs[item["name"]]
            # Kubernetes adds only these defaults outside the maintained spec.
            for key, default in (
                ("conversion", {"strategy": "None"}),
                ("preserveUnknownFields", False),
            ):
                if key not in desired and spec.get(key) == default:
                    spec.pop(key)
            stored = actual.get("status", {}).get("storedVersions")
            served = {
                version["name"]
                for version in desired["versions"]
                if version.get("served") is True
            }
            storage = {
                version["name"]
                for version in desired["versions"]
                if version.get("storage") is True
            }
            if (
                not isinstance(stored, list)
                or not stored
                or not storage.issubset(set(stored))
                or not set(stored).issubset(served)
            ):
                raise BootstrapRefused("shared CRD stored versions are incompatible")
            if (
                actual["metadata"].get("uid") != item["uid"]
                or digest(spec) != item["spec_sha256"]
                or not any(
                    c.get("type") == "Established" and c.get("status") == "True"
                    for c in actual.get("status", {}).get("conditions", [])
                )
            ):
                raise BootstrapRefused(
                    "shared CRD identity, schema or establishment changed"
                )
        controller = descriptor["controller"]
        namespace = self._read(self.management, "Namespace", controller["namespace"])
        deployment = self._read(
            self.management,
            "Deployment",
            controller["deployment"],
            controller["namespace"],
        )
        meta, spec, status = (
            deployment["metadata"],
            deployment.get("spec", {}),
            deployment.get("status", {}),
        )
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        if (
            namespace["metadata"].get("uid") != controller["namespace_uid"]
            or namespace.get("status", {}).get("phase") != "Active"
            or meta.get("uid") != controller["deployment_uid"]
            or digest(spec.get("template")) != controller["template_sha256"]
            or [
                c.get("image")
                for c in containers
                if c.get("name") == controller["container"]
            ]
            != [controller["image"]]
            or not meta.get("generation")
            or status.get("observedGeneration", 0) < meta["generation"]
            or spec.get("replicas", 0) <= 0
            or any(
                status.get(key, 0) != spec["replicas"]
                for key in ("updatedReplicas", "availableReplicas", "readyReplicas")
            )
        ):
            raise BootstrapRefused(
                "shared management controller identity or readiness changed"
            )
        pinned = (
            controller["namespace"],
            controller["deployment"],
            controller["deployment_uid"],
            controller["container"],
        )
        if self._controllers(self.management) != [pinned]:
            raise BootstrapRefused(
                "management cluster has another controller or lost its sole controller"
            )
        same_cluster = (
            self.issuer.target.cluster_arn == self.management.target.cluster_arn
        )
        if self._controllers(self.issuer) != ([pinned] if same_cluster else []):
            raise BootstrapRefused("shared target has a competing workspace controller")
        self.verify()
