"""Independent EC2/ASG evidence for the IPv4-only metadata qualification case."""
from datetime import datetime, timezone
import re


def require(condition):
    if not condition:
        raise ValueError("ipv6_metadata_provider_evidence_invalid")


def enabled(fixtures):
    return any(row.get("name") == "metadata_ipv6" and row.get("expected") == "disabled_by_provider"
               for row in fixtures.get("network_targets", []))


def instance_identity(instance):
    options = instance.get("MetadataOptions", {})
    require(options.get("HttpProtocolIpv6") == "disabled" and options.get("State") == "applied"
            and options.get("HttpTokens") == "required"
            and not instance.get("IamInstanceProfile") and not instance.get("PublicIpAddress"))
    return {"instance_id": instance["InstanceId"], "ami_id": instance["ImageId"],
            "vpc_id": instance["VpcId"], "subnet_id": instance["SubnetId"],
            "metadata_ipv6": options["HttpProtocolIpv6"], "metadata_state": options["State"],
            "http_tokens": options["HttpTokens"], "instance_profile": None}


def instance_facts(instance):
    interfaces = instance.get("NetworkInterfaces")
    require(isinstance(interfaces, list) and interfaces
            and all(row.get("NetworkInterfaceId") and row.get("Ipv6Addresses") == []
                    and not row.get("Association", {}).get("PublicIp") for row in interfaces))
    return {**instance_identity(instance),
            "interfaces": sorted([{"id": row["NetworkInterfaceId"], "ipv6_addresses": []}
                                  for row in interfaces], key=lambda row: row["id"])}


def collect(fixtures, instance_id, ec2, autoscaling):
    """Collect while the instance is still in its owned ASG, before teardown."""
    started = datetime.now(timezone.utc).isoformat()
    response = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [row for reservation in response["Reservations"] for row in reservation["Instances"]]
    require(len(instances) == 1 and instances[0]["InstanceId"] == instance_id)
    instance = instance_facts(instances[0])
    require(instance["vpc_id"] == fixtures["vpc_id"] and instance["subnet_id"] in fixtures["private_subnet_ids"]
            and instance["ami_id"] == fixtures["ami_id"])
    membership = autoscaling.describe_auto_scaling_instances(InstanceIds=[instance_id])
    members = membership["AutoScalingInstances"]
    require(len(members) == 1 and members[0]["InstanceId"] == instance_id
            and members[0]["AutoScalingGroupName"] in fixtures["asg_names"])
    binding = members[0].get("LaunchTemplate", {})
    require(re.fullmatch(r"lt-[0-9a-f]+", binding.get("LaunchTemplateId", ""))
            and re.fullmatch(r"[1-9][0-9]*", binding.get("Version", "")))
    template_response = ec2.describe_launch_template_versions(
        LaunchTemplateId=binding["LaunchTemplateId"], Versions=[binding["Version"]])
    templates = template_response["LaunchTemplateVersions"]
    require(len(templates) == 1 and templates[0]["LaunchTemplateId"] == binding["LaunchTemplateId"]
            and templates[0]["VersionNumber"] == int(binding["Version"]))
    data = templates[0]["LaunchTemplateData"]
    require(data.get("ImageId") == fixtures["ami_id"] and not data.get("IamInstanceProfile")
            and data.get("MetadataOptions", {}).get("HttpProtocolIpv6") == "disabled"
            and data.get("MetadataOptions", {}).get("HttpTokens") == "required")
    request_ids = {name: value.get("ResponseMetadata", {}).get("RequestId") for name, value in (
        ("ec2_instance", response), ("asg_membership", membership), ("ec2_launch_template", template_response))}
    require(all(isinstance(value, str) and value for value in request_ids.values()))
    return {"source": "ec2_and_autoscaling_api", "result": "disabled_by_provider",
            "run_id": fixtures["run_id"], "source_revision": fixtures["source_revision"],
            "account_id": fixtures["account_id"], "started_at": started,
            "observed_at": datetime.now(timezone.utc).isoformat(), "aws_request_ids": request_ids,
            "instance": instance, "asg_name": members[0]["AutoScalingGroupName"],
            "launch_template": {"id": binding["LaunchTemplateId"], "version": int(binding["Version"]),
                                "ami_id": data["ImageId"], "metadata_ipv6": "disabled",
                                "http_tokens": "required", "instance_profile": None}}


def current_instance_matches(instance, retained):
    # EC2 may remove ENI details after termination. Their pre-cleanup receipt
    # remains mandatory; absence on a running host cannot use this path.
    if not instance.get("NetworkInterfaces"):
        require(instance.get("State", {}).get("Name") == "terminated")
        require(instance_identity(instance) == {key: value for key, value in retained.items() if key != "interfaces"})
    else:
        require(instance_facts(instance) == retained)


def validate(receipt, fixtures, instance_id, start, end):
    """Reject stale, partial or differently scoped retained provider observations."""
    def moment(value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None)
        return parsed

    require(receipt.get("source") == "ec2_and_autoscaling_api"
            and receipt.get("result") == "disabled_by_provider"
            and all(receipt.get(key) == fixtures[key] for key in ("run_id", "source_revision", "account_id"))
            and start <= moment(receipt["started_at"]) <= moment(receipt["observed_at"]) <= end
            and (moment(receipt["observed_at"]) - moment(receipt["started_at"])).total_seconds() <= 30)
    ids = receipt.get("aws_request_ids", {})
    require(set(ids) == {"ec2_instance", "asg_membership", "ec2_launch_template"}
            and all(isinstance(value, str) and value for value in ids.values()))
    instance, template = receipt["instance"], receipt["launch_template"]
    require(instance["instance_id"] == instance_id and instance["vpc_id"] == fixtures["vpc_id"]
            and instance["subnet_id"] in fixtures["private_subnet_ids"]
            and instance["ami_id"] == fixtures["ami_id"] == template["ami_id"]
            and instance["metadata_ipv6"] == template["metadata_ipv6"] == "disabled"
            and instance["metadata_state"] == "applied"
            and instance["http_tokens"] == template["http_tokens"] == "required"
            and instance["instance_profile"] is None and template["instance_profile"] is None
            and instance["interfaces"] and all(row["id"] and row["ipv6_addresses"] == [] for row in instance["interfaces"]))
    require(receipt["asg_name"] in fixtures["asg_names"]
            and re.fullmatch(r"lt-[0-9a-f]+", template["id"])
            and type(template["version"]) is int and template["version"] > 0)
