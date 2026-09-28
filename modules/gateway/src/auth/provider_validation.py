"""Independent AWS EC2 observations made inside Gateway, using a pinned secret.

No report fields come from the owner request. Instance placement is a server-held
deployment profile. EC2 RunInstances is always DryRun; no resource is created.
Quota and capacity remain separate: quota is measured, fleet capacity is unknown.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field

from src.internal.sts_assume_service import STSAssumeError, assume_role


class ValidationUnavailableError(RuntimeError):
    pass


class AwsValidationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    region: str = Field(pattern=r"^[a-z]{2}(?:-[a-z]+)+-\d+$")
    image_id: str = Field(pattern=r"^ami-[0-9a-f]+$")
    instance_type: str = Field(pattern=r"^[a-z][a-z0-9-]*\.[a-z0-9]+$")
    subnet_id: str = Field(pattern=r"^subnet-[0-9a-f]+$")
    security_group_ids: list[Annotated[str, Field(pattern=r"^sg-[0-9a-f]+$")]] = Field(min_length=1, max_length=5)


@dataclass(frozen=True)
class ProviderReading:
    credential_valid: bool
    permissions_sufficient: bool
    quota_available: bool
    checked_at: datetime
    observed_capacity: int | None = None
    detail: str = "aws-ec2-readonly-validation/v1; physical capacity not measured"
    provider_account_id: str | None = None


def _quota_group(instance_type):
    family = instance_type.split(".")[0]
    if re.fullmatch(r"(?:g|vt)[0-9]+[a-z-]*", family):
        return "L-DB2E81BA"
    if re.fullmatch(r"p[0-9]+[a-z-]*", family):
        return "L-417A185B"
    if re.fullmatch(r"[acdhimrtz][0-9]+[a-z-]*", family):
        return "L-1216C47"
    return None


class AwsEc2Validator:
    def __init__(self, profile, *, session_factory=None):
        self.profile = profile
        self.session_factory = session_factory or boto3.Session
        if not _quota_group(profile.instance_type):
            raise ValidationUnavailableError("unsupported EC2 quota family")

    def validate(self, material, *, credential_type, user_id, label):
        try:
            data = json.loads(material)
            if credential_type != "aws_role" or not isinstance(data, dict) or not data.get("role_arn"):
                raise ValidationUnavailableError("AWS validation requires an ADP AWS role credential")
            # Reuse the existing tagged assume workflow; retain user attribution.
            credentials = assume_role(
                role_arn=data["role_arn"],
                external_id=data.get("external_id"),
                session_duration_seconds=900,
                default_region=self.profile.region,
                user_id=user_id,
                agent_id="vault-validator",
                task_id="validate",
                label=label,
                aws_region=self.profile.region,
            )
            session = self.session_factory(
                aws_access_key_id=credentials.access_key_id,
                aws_secret_access_key=credentials.secret_access_key,
                aws_session_token=credentials.session_token,
                region_name=self.profile.region,
            )
            config = Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2, "mode": "standard"})
            account = session.client("sts", config=config).get_caller_identity()["Account"]
            if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
                raise ValueError("invalid caller identity")
        except (STSAssumeError, ClientError):
            return ProviderReading(False, False, False, datetime.now(UTC))
        except (ValueError, KeyError, TypeError, BotoCoreError):
            raise ValidationUnavailableError("provider validation unavailable") from None

        ec2 = session.client("ec2", config=config)
        try:
            ec2.run_instances(
                ImageId=self.profile.image_id,
                InstanceType=self.profile.instance_type,
                SubnetId=self.profile.subnet_id,
                SecurityGroupIds=self.profile.security_group_ids,
                MinCount=1,
                MaxCount=1,
                DryRun=True,
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "DryRunOperation":
                permitted = True
            elif code in {"UnauthorizedOperation", "AccessDenied", "AccessDeniedException"}:
                permitted = False
            else:
                raise ValidationUnavailableError("EC2 permission validation unavailable") from None
        except BotoCoreError:
            raise ValidationUnavailableError("EC2 permission validation unavailable") from None
        else:
            # A successful launch response to a DryRun request is not a dry-run
            # attestation. Never label it validated or attempt a second mutation.
            raise ValidationUnavailableError("EC2 dry-run proof unavailable")
        try:
            group = _quota_group(self.profile.instance_type)
            quota = session.client("service-quotas", config=config).get_service_quota(ServiceCode="ec2", QuotaCode=group)["Quota"]["Value"]
            if isinstance(quota, bool) or not isinstance(quota, int | float) or not 0 <= quota < float("inf"):
                raise ValueError("invalid quota")
            counts = {}
            pages = ec2.get_paginator("describe_instances").paginate(Filters=[{"Name": "instance-state-name", "Values": ["pending", "running"]}])
            for page_index, page in enumerate(pages):
                if page_index >= 100:
                    raise ValueError("inventory bound exceeded")
                for reservation in page["Reservations"]:
                    for instance in reservation["Instances"]:
                        kind = instance["InstanceType"]
                        if not instance.get("InstanceLifecycle") and _quota_group(kind) == group:
                            counts[kind] = counts.get(kind, 0) + 1
            # Unused active On-Demand Capacity Reservations also consume this
            # account's vCPU quota. Occupied slots are already in the inventory.
            reservations = ec2.get_paginator("describe_capacity_reservations").paginate(Filters=[{"Name": "state", "Values": ["active"]}])
            for page_index, page in enumerate(reservations):
                if page_index >= 100:
                    raise ValueError("reservation bound exceeded")
                for reservation in page["CapacityReservations"]:
                    if reservation["OwnerId"] != account or reservation["State"] != "active":
                        continue
                    kind = reservation["InstanceType"]
                    available = reservation["AvailableInstanceCount"]
                    if type(available) is not int or available < 0:
                        raise ValueError("invalid reservation sizing")
                    if _quota_group(kind) == group:
                        counts[kind] = counts.get(kind, 0) + available
            kinds = sorted(set(counts) | {self.profile.instance_type})
            vcpus = {}
            for offset in range(0, len(kinds), 100):
                reply = ec2.describe_instance_types(InstanceTypes=kinds[offset : offset + 100])
                for item in reply["InstanceTypes"]:
                    value = item["VCpuInfo"]["DefaultVCpus"]
                    if type(value) is not int or value < 1:
                        raise ValueError("invalid instance sizing")
                    vcpus[item["InstanceType"]] = value
            in_use = sum(count * vcpus[kind] for kind, count in counts.items())
            available = in_use + vcpus[self.profile.instance_type] <= quota
        except (ClientError, BotoCoreError, KeyError, ValueError, TypeError):
            raise ValidationUnavailableError("EC2 quota validation unavailable") from None
        return ProviderReading(True, permitted, available, datetime.now(UTC), provider_account_id=account)


def configured_validator(settings, *, org_id, workspace_id, service):
    """Profiles are keyed by tenant and workspace, never supplied by an HTTP body."""
    try:
        profiles = json.loads(settings.credential_validation_profiles)
        profile = profiles[org_id][workspace_id]
        if service != "aws":
            raise ValueError("unsupported provider")
        return AwsEc2Validator(AwsValidationProfile.model_validate(profile))
    except (ValueError, KeyError, TypeError):
        raise ValidationUnavailableError("provider validation is not configured for this workspace") from None
