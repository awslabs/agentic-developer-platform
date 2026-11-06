"""Protected installation routing, never a Product or provider request option."""

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TransformProviderEndpoint:
    account_id: str
    region: str
    vpc_id: str
    endpoint_id: str
    dns_name: str

    @property
    def url(self):
        return "https://" + self.dns_name

    def as_json(self):
        return json.dumps({"schema_version": 1, "account_id": self.account_id, "region": self.region,
            "vpc_id": self.vpc_id, "endpoint_id": self.endpoint_id, "dns_name": self.dns_name,
            "service_name": f"com.amazonaws.{self.region}.transform-custom"}, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_installed(cls, raw, *, region, provider_image):
        """Bind a bootstrap readback to its installed image account and region."""
        try:
            value = json.loads(raw)
            image = re.fullmatch(r"([0-9]{12})\.dkr\.ecr\.([a-z]{2}-[a-z]+-[0-9])\.amazonaws\.com/"
                                 r"[a-z0-9._/-]+@sha256:[a-f0-9]{64}", provider_image)
            if (not isinstance(value, dict) or set(value) != {
                    "schema_version", "account_id", "region", "vpc_id", "endpoint_id", "dns_name", "service_name"}
                    or type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or not image or image.group(1) != value["account_id"]
                    or image.group(2) != region or value["region"] != region
                    or value["service_name"] != f"com.amazonaws.{region}.transform-custom"
                    or not re.fullmatch(r"vpc-[a-f0-9]{17}", value["vpc_id"])
                    or not re.fullmatch(r"vpce-[a-f0-9]{17}", value["endpoint_id"])
                    or not re.fullmatch(re.escape(value["endpoint_id"]) + r"-[a-z0-9]+\.transform-custom\."
                                        + re.escape(region) + r"\.vpce\.amazonaws\.com", value["dns_name"])):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("L5_TRANSFORM_PROVIDER_ENDPOINT_REF must select the installed regional AWS interface endpoint") from None
        return cls(**{key: value[key] for key in ("account_id", "region", "vpc_id", "endpoint_id", "dns_name")})
