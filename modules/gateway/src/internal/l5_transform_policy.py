"""Finite Transform custom sessions for the exact admitted definition.

AWS documents package resources for ExecuteTransformation and
GetTransformationPackageUrl, but no resource type for ConverseStream. Only that
single streaming action uses '*', bounded by the selected region and work lease.
Source reads remain on Product snapshot APIs; no S3, SCM, IAM or executor rights
are included in the provider session.
"""

import json
import re

from fastapi import HTTPException
from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256, NDJSON_INTERFACE
from l5_services.adapters.transform.qualification import ProviderQualification
from l5_services.transform_contract import TransformSettings


def _denied():
    return HTTPException(409, detail={"code": "transform_scope_unverified"})


def _selection(settings, identity, scope):
    try:
        selected = TransformSettings.model_validate(settings["transform"]).model_dump(exclude_none=True)
        definition = selected["definition_ref"]
        connection = selected["connection_ref"]
        account, region, role = (identity.get(field) for field in ("account_id", "region", "role_arn"))
        if (
            not isinstance(account, str)
            or not re.fullmatch(r"[0-9]{12}", account)
            or not isinstance(region, str)
            or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region)
            or region != selected["region"]
            or not isinstance(role, str)
            or not re.fullmatch(rf"arn:aws:iam::{account}:role/[A-Za-z0-9+=,.@_/-]+", role)
            or definition["record_type"] != "transform_definition"
            or definition["source_connection_id"] != connection["source_connection_id"]
            or any(definition[field] != scope[field] or connection[field] != scope[field] for field in ("tenant_id", "project_id"))
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9_.-]{0,99})?", definition["record_id"])
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", definition["record_revision"])
            or definition["record_revision"] in {"HEAD", "latest", "null"}
        ):
            raise ValueError("selected definition or provider differs")
        return selected, f"arn:aws:transform-custom:{region}:{account}:package/{definition['record_id']}"
    except (ValueError, KeyError, TypeError):
        raise _denied() from None


def setup_policy(settings, intent, identity, expires_at):
    selected, package = _selection(settings, identity, intent.scope)
    if (
        intent.service_id != "transform"
        or intent.request.get("action") != "link"
        or intent.request.get("native_resource_ref") != selected["definition_ref"]
    ):
        raise _denied()
    return _policy(package, selected["region"], expires_at, execute=False)


def run_policy(settings, binding, run, expires_at):
    selected, package = _selection(settings, binding.provider_identity or {}, binding.scope)
    try:
        qualification = ProviderQualification.model_validate(run.admission["transform_qualification"])
        if (
            run.service_id != "transform"
            or run.input.get("capability") not in {"refactor", "dependency_upgrade"}
            or run.input.get("capability") not in settings.get("capabilities", [])
            or run.admission.get("transform_settings") != selected
            or run.admission.get("qualified_executor_ref") != selected["executor_binding_ref"]
            or run.admission.get("provider_identity") != binding.provider_identity
            or binding.native_resource_ref != selected["definition_ref"]
            or not qualification.ready_for_submission
            or qualification.interface != NDJSON_INTERFACE
            or qualification.cli_version != "3.19.0"
            or qualification.binary_sha256 != NDJSON_BINARY_SHA256
            or qualification.definition_revision != selected["definition_ref"]["record_revision"]
        ):
            raise ValueError("admitted Transform selection differs")
    except (ValueError, KeyError, TypeError):
        raise _denied() from None
    return _policy(package, selected["region"], expires_at, execute=True)


def _policy(package, region, expires_at, *, execute):
    condition = {"DateLessThan": {"aws:CurrentTime": expires_at.isoformat()}, "StringEquals": {"aws:RequestedRegion": region}}
    statements = [{"Effect": "Allow", "Action": ["transform-custom:GetTransformationPackageUrl"], "Resource": package, "Condition": condition}]
    if execute:
        statements[0]["Action"].append("transform-custom:ExecuteTransformation")
        statements.append({"Effect": "Allow", "Action": ["transform-custom:ConverseStream"], "Resource": "*", "Condition": condition})
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))
