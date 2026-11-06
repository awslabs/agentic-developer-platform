#!/usr/bin/env python3
"""Retain native signer provisioning without printing Terraform state."""

import json
import sys


def native_credential_settings(state):
    """An existing owner key survives a targeted apply that omitted outputs."""
    saved = state.get("outputs", {}).get("release_configuration", {}).get("value", {})
    if not isinstance(saved, dict):
        raise ValueError("Invalid retained native credential configuration")
    flag = "native_credentials_enabled"
    if flag in saved and type(saved[flag]) is not bool:
        raise ValueError("Invalid retained native credential enablement")
    keys = [instance["attributes"] for resource in state.get("resources", [])
            if resource.get("mode") == "managed" and resource.get("module") == "module.native_credentials"
            and resource.get("type") == "aws_kms_key" and resource.get("name") == "issuer"
            for instance in resource.get("instances", [])]
    if keys:
        if (len(keys) != 1 or keys[0].get("key_usage") != "SIGN_VERIFY"
                or keys[0].get("customer_master_key_spec") != "RSA_3072"):
            raise ValueError("Existing native credential signer requires owner review")
        # A reviewed targeted enablement can leave an older false root output.
        # The actual owned signer is stronger evidence of provisioning; this
        # retains it without interpreting a stale output as a removal request.
        return {flag: True}
    return {flag: saved[flag]} if flag in saved else {}


def from_show(document):
    """Normalize `terraform show -json`, including a genuinely empty backend."""
    if not isinstance(document, dict) or document.get("format_version") != "1.0":
        raise ValueError("Invalid native credential owner state")
    if "values" not in document:
        if set(document) != {"format_version"}:
            raise ValueError("Invalid native credential owner state")
        return {}
    values = document["values"]
    resources = []

    def visit(module):
        for resource in module.get("resources", []):
            # Match the immutable owner address, not a key's display name, tags
            # or a similarly named key in another Terraform module.
            if resource.get("address") == "module.native_credentials.aws_kms_key.issuer[0]" and resource.get("mode") == "managed":
                resources.append({"mode": "managed", "module": "module.native_credentials", "type": "aws_kms_key", "name": "issuer",
                                  "instances": [{"attributes": resource["values"]}]})
        for child in module.get("child_modules", []):
            visit(child)

    visit(values.get("root_module", {}))
    return {"outputs": values.get("outputs", {}), "resources": resources}


def main():
    try:
        # Only the selected boolean leaves this process. No state, key material,
        # secrets, users, entitlements, approvals or consents are synthesized.
        print(json.dumps(native_credential_settings(from_show(json.load(sys.stdin)))))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SystemExit("Cannot preserve native credential owner inputs; review the existing signer configuration") from None


if __name__ == "__main__":
    main()
