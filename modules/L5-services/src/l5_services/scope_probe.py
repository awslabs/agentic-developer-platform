"""Fail-closed predicates for independently collected, authorized scope evidence."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from l5_services.installed_release_probe import digest, require
from l5_services.recovery_probe import timestamp

CALL_SOURCES = {
    "api": "authenticated-api",
    "source": "native-storage",
    "artifact": "native-artifact",
    "provider": "native-provider",
    "queue": "aws-sqs",
}
SURFACES = ("api", "source", "artifact", "provider", "queue")
NETWORK_ACTIONS = (
    "ec2:CreateNetworkInterface",
    "ec2:DescribeNetworkInterfaces",
    "ec2:DeleteNetworkInterface",
    "ec2:AssignPrivateIpAddresses",
    "ec2:UnassignPrivateIpAddresses",
)
SOURCES = {
    "identity": "aws-sts",
    "grants": "product-auth",
    "policies": "aws-iam",
    "calls": "native-calls",
    "provider_absence": "native-provider",
    "database_absence": "database",
    "budget": "budget-meter",
    "cleanup": "native-cleanup",
}
QUEUE_ACTIONS = {
    "gateway": {"sqs:SendMessage"},
    "worker": {
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ChangeMessageVisibility",
        "sqs:SendMessage",
    },
    "repair": {"sqs:SendMessage"},
}


def text(value):
    return isinstance(value, str) and 0 < len(value) <= 256


def money(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"(?:0|[1-9][0-9]{0,3})\.[0-9]{2}", value) is not None
    )


@dataclass(frozen=True)
class ScopeFixture:
    account_id: str
    role_arn: str
    gateway_role_arn: str
    queue_arn: str
    allowed_tenant: str
    foreign_tenant: str
    allowed_product: str
    foreign_product: str
    allowed_grant: str
    foreign_grant: str
    probe_id: str
    principals: dict
    targets: dict
    policy_sha256: dict
    boundary_sha256: dict
    owned_resources: tuple[str, ...]
    deadline_seconds: int
    max_attempts: int
    max_spend_usd: str

    @classmethod
    def from_manifest(cls, path: Path):
        manifest = json.loads(path.read_text())
        require(
            isinstance(manifest, dict)
            and type(manifest.get("schema_version")) is int
            and manifest["schema_version"] == 1,
            "scope_fixture_version_invalid",
        )
        data = manifest.get("effective_scope")
        require(
            isinstance(data, dict) and set(data) == set(cls.__dataclass_fields__),
            "scope_fixture_fields_invalid",
        )
        require(
            isinstance(data["owned_resources"], list)
            and bool(data["owned_resources"])
            and all(text(item) for item in data["owned_resources"])
            and len(data["owned_resources"]) == len(set(data["owned_resources"])),
            "scope_fixture_resources_invalid",
        )
        fixture = cls(**{**data, "owned_resources": tuple(data["owned_resources"])})
        require(
            isinstance(fixture.account_id, str)
            and re.fullmatch(r"[0-9]{12}", fixture.account_id) is not None,
            "scope_fixture_account_invalid",
        )
        require(
            isinstance(fixture.role_arn, str)
            and re.fullmatch(
                rf"arn:aws:iam::{fixture.account_id}:role/[A-Za-z0-9+=,.@_/-]+",
                fixture.role_arn,
            )
            is not None,
            "scope_fixture_role_invalid",
        )
        require(
            isinstance(fixture.queue_arn, str)
            and re.fullmatch(
                rf"arn:aws:sqs:[a-z0-9-]+:{fixture.account_id}:adp-(dev|test|prod)-l5-work",
                fixture.queue_arn,
            )
            is not None,
            "scope_fixture_queue_invalid",
        )
        require(
            isinstance(fixture.gateway_role_arn, str)
            and re.fullmatch(
                rf"arn:aws:iam::{fixture.account_id}:role/[A-Za-z0-9+=,.@_/-]+",
                fixture.gateway_role_arn,
            )
            is not None,
            "scope_fixture_role_invalid",
        )
        for allowed, foreign in (
            (fixture.allowed_tenant, fixture.foreign_tenant),
            (fixture.allowed_product, fixture.foreign_product),
            (fixture.allowed_grant, fixture.foreign_grant),
        ):
            require(
                text(allowed) and text(foreign) and allowed != foreign,
                "scope_fixture_grants_invalid",
            )
        require(
            text(fixture.probe_id)
            and isinstance(fixture.principals, dict)
            and set(fixture.principals) == set(SURFACES)
            and all(text(item) for item in fixture.principals.values()),
            "scope_fixture_principals_invalid",
        )
        environment = (
            fixture.queue_arn.rsplit(":", 1)[-1]
            .removeprefix("adp-")
            .removesuffix("-l5-work")
        )
        require(
            fixture.principals["queue"]
            == f"arn:aws:iam::{fixture.account_id}:role/adp-{environment}-l5-worker",
            "scope_fixture_principals_invalid",
        )
        require(
            isinstance(fixture.targets, dict) and set(fixture.targets) == set(SURFACES),
            "scope_fixture_targets_invalid",
        )
        for surface, scoped in fixture.targets.items():
            require(
                isinstance(scoped, dict) and set(scoped) == {"allowed", "foreign"},
                "scope_fixture_targets_invalid",
            )
            permitted, denied = scoped["allowed"], scoped["foreign"]
            require(
                all(
                    isinstance(item, dict)
                    and set(item) == {"operation", "resource"}
                    and all(text(value) and "*" not in value for value in item.values())
                    for item in (permitted, denied)
                )
                and permitted["resource"] != denied["resource"]
                and permitted["operation"] == denied["operation"],
                "scope_fixture_targets_invalid",
            )
            if surface == "queue":
                require(
                    permitted["resource"] == fixture.queue_arn
                    and denied["resource"] != fixture.queue_arn
                    and permitted["operation"] in QUEUE_ACTIONS["worker"]
                    and re.fullmatch(
                        rf"arn:aws:sqs:{fixture.queue_arn.split(':')[3]}:{fixture.account_id}:[A-Za-z0-9_-]+",
                        denied["resource"],
                    )
                    is not None,
                    "scope_fixture_targets_invalid",
                )
        require(
            isinstance(fixture.policy_sha256, dict)
            and set(fixture.policy_sha256) == set(QUEUE_ACTIONS)
            and all(digest(item) for item in fixture.policy_sha256.values()),
            "scope_fixture_policy_invalid",
        )
        require(
            isinstance(fixture.boundary_sha256, dict)
            and set(fixture.boundary_sha256) == {"worker", "repair"}
            and all(digest(item) for item in fixture.boundary_sha256.values()),
            "scope_fixture_boundary_invalid",
        )
        require(
            type(fixture.deadline_seconds) is int
            and 1 <= fixture.deadline_seconds <= 900
            and type(fixture.max_attempts) is int
            and 1 <= fixture.max_attempts <= 3
            and money(fixture.max_spend_usd)
            and Decimal(fixture.max_spend_usd) > 0,
            "scope_fixture_limits_invalid",
        )
        return fixture


def sha256_document(document):
    require(isinstance(document, dict), "scope_policy_missing")
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def check_policies(fixture, policies):
    roles = policies.get("roles")
    require(
        isinstance(roles, dict) and set(roles) == set(QUEUE_ACTIONS),
        "scope_roles_missing",
    )
    region = fixture.queue_arn.split(":")[3]
    environment = (
        fixture.queue_arn.rsplit(":", 1)[-1]
        .removeprefix("adp-")
        .removesuffix("-l5-work")
    )
    for label, queue_actions in QUEUE_ACTIONS.items():
        part = roles[label]
        require(isinstance(part, Mapping), "scope_role_missing")
        role_name = f"adp-{environment}-l5-{label}" if label != "gateway" else None
        if role_name:
            require(
                part.get("role_arn")
                == f"arn:aws:iam::{fixture.account_id}:role/{role_name}"
                and part.get("managed_policies") == [],
                "scope_role_changed",
            )
        else:
            require(
                text(part.get("role_arn"))
                and part["role_arn"].startswith(
                    f"arn:aws:iam::{fixture.account_id}:role/"
                ),
                "scope_role_changed",
            )
        policy = part.get("policy")
        require(
            sha256_document(policy) == fixture.policy_sha256[label],
            "scope_policy_changed",
        )
        statements = policy.get("Statement")
        require(
            isinstance(statements, list)
            and bool(statements)
            and all(isinstance(item, Mapping) for item in statements),
            "scope_policy_changed",
        )
        function_arn = (
            f"arn:aws:lambda:{region}:{fixture.account_id}:function:{role_name}"
        )
        queue_statement = {
            "Effect": "Allow",
            "Action": sorted(queue_actions),
            "Resource": fixture.queue_arn,
        }
        if label == "gateway":
            expected = [queue_statement]
            require(
                part.get("role_arn") == fixture.gateway_role_arn, "scope_role_changed"
            )
        else:
            expected = [
                {
                    "Effect": "Allow",
                    "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                    "Resource": f"arn:aws:logs:{region}:{fixture.account_id}:log-group:/aws/lambda/{role_name}:*",
                },
                queue_statement,
                {"Effect": "Allow", "Action": list(NETWORK_ACTIONS), "Resource": "*"},
                {
                    "Effect": "Deny",
                    "Action": list(NETWORK_ACTIONS),
                    "Resource": "*",
                    "Condition": {
                        "ArnEquals": {"lambda:SourceFunctionArn": function_arn}
                    },
                },
            ]
        require(
            policy.get("Version") == "2012-10-17"
            and set(policy) == {"Version", "Statement"}
            and len(statements) == len(expected)
            and all(
                isinstance(item.get("Action"), list)
                and all(isinstance(action, str) for action in item["Action"])
                for item in statements
            ),
            "scope_policy_expanded",
        )
        normalized = [{**item, "Action": sorted(item["Action"])} for item in statements]
        expected_normalized = [
            {**item, "Action": sorted(item["Action"])} for item in expected
        ]
        require(
            sorted(json.dumps(item, sort_keys=True) for item in normalized)
            == sorted(json.dumps(item, sort_keys=True) for item in expected_normalized),
            "scope_policy_expanded",
        )
        if label != "gateway":
            boundary = part.get("boundary")
            require(
                isinstance(boundary, Mapping)
                and isinstance(boundary.get("arn"), str)
                and re.fullmatch(
                    rf"arn:aws:iam::{fixture.account_id}:policy/[A-Za-z0-9_/-]+",
                    boundary["arn"],
                )
                is not None
                and sha256_document(boundary.get("document"))
                == fixture.boundary_sha256[label]
                and text(boundary.get("version_id")),
                "scope_boundary_changed",
            )
            boundary_document = boundary["document"]
            boundary_statements = boundary_document.get("Statement")
            log_resource = f"arn:aws:logs:{region}:{fixture.account_id}:log-group:/aws/lambda/{role_name}:*"
            permitted_resources = {
                fixture.queue_arn: queue_actions,
                log_resource: {"logs:CreateLogStream", "logs:PutLogEvents"},
                "*": set(NETWORK_ACTIONS),
            }
            require(
                boundary_document.get("Version") == "2012-10-17"
                and isinstance(boundary_statements, list)
                and bool(boundary_statements)
                and all(
                    isinstance(item, Mapping)
                    and item.get("Effect") in {"Allow", "Deny"}
                    for item in boundary_statements
                )
                and all(
                    item.get("Effect") != "Allow"
                    or (
                        isinstance(item.get("Action"), list)
                        and bool(item["Action"])
                        and item.get("Resource") in permitted_resources
                        and set(item["Action"]) <= permitted_resources[item["Resource"]]
                    )
                    for item in boundary_statements
                ),
                "scope_boundary_expanded",
            )
            require(
                part.get("function_arn")
                == f"arn:aws:lambda:{region}:{fixture.account_id}:function:{role_name}",
                "scope_role_changed",
            )


def verify_effective_scope(fixture: ScopeFixture, observed: Mapping, *, now=None):
    """Verify supplied native observations, not a policy simulation or a live probe."""
    now = datetime.now(UTC) if now is None else now
    require(
        isinstance(now, datetime) and now.utcoffset() is not None, "scope_clock_invalid"
    )
    require(
        isinstance(observed, Mapping)
        and all(isinstance(observed.get(name), Mapping) for name in SOURCES),
        "scope_readback_missing",
    )
    references = set()
    for name, source in SOURCES.items():
        part = observed[name]
        require(part.get("source") == source, f"scope_{name}_source_invalid")
        timestamp(part.get("observed_at"), now, f"scope_{name}_time_invalid")
        reference = part.get("evidence_ref")
        require(
            text(reference) and reference not in references,
            f"scope_{name}_reference_invalid",
        )
        references.add(reference)
    identity = observed["identity"]
    require(
        identity.get("account_id") == fixture.account_id
        and identity.get("role_arn") == fixture.role_arn,
        "scope_target_changed",
    )
    grants = observed["grants"]
    require(
        grants.get("allowed")
        == {
            "tenant": fixture.allowed_tenant,
            "product": fixture.allowed_product,
            "grant": fixture.allowed_grant,
            "current": True,
        }
        and grants.get("foreign")
        == {
            "tenant": fixture.foreign_tenant,
            "product": fixture.foreign_product,
            "grant": fixture.foreign_grant,
            "current": False,
        },
        "scope_grants_changed",
    )
    check_policies(fixture, observed["policies"])
    calls = observed["calls"].get("results")
    require(
        isinstance(calls, list) and len(calls) == 2 * len(SURFACES),
        "scope_calls_incomplete",
    )
    seen = set()
    call_times = []
    for call in calls:
        require(
            isinstance(call, Mapping)
            and call.get("surface") in SURFACES
            and call.get("scope") in {"allowed", "foreign"},
            "scope_call_invalid",
        )
        surface, scope = call["surface"], call["scope"]
        key = surface, scope
        require(key not in seen, "scope_call_replayed")
        seen.add(key)
        expected = fixture.targets[surface][scope]
        require(
            call.get("operation") == expected["operation"]
            and call.get("resource") == expected["resource"]
            and call.get("principal") == fixture.principals[surface]
            and call.get("source") == CALL_SOURCES[surface],
            "scope_call_target_changed",
        )
        require(
            call.get("tenant") == getattr(fixture, f"{scope}_tenant")
            and call.get("product") == getattr(fixture, f"{scope}_product"),
            "scope_call_product_changed",
        )
        reference = call.get("evidence_ref")
        require(
            text(reference) and reference not in references,
            "scope_call_reference_invalid",
        )
        references.add(reference)
        call_times.append(
            timestamp(call.get("observed_at"), now, "scope_call_time_invalid")
        )
        if scope == "foreign":
            require(call.get("disclosed_resources") == [], "scope_foreign_disclosed")
        if scope == "allowed":
            require(
                call.get("result") == "allowed"
                and type(call.get("http_status")) is int
                and 200 <= call["http_status"] < 300
                and call.get("error_code") is None,
                "scope_allowed_failed",
            )
        elif surface == "api":
            require(
                call.get("result") == "denied"
                and call.get("http_status") in (403, 404)
                and call.get("error_code") is None,
                "scope_foreign_disclosed",
            )
        else:
            require(
                call.get("result") == "denied"
                and call.get("error_code") == "AccessDenied"
                and call.get("http_status") is None,
                "scope_foreign_permitted",
            )
    require(
        seen
        == {
            (surface, scope) for surface in SURFACES for scope in ("allowed", "foreign")
        },
        "scope_calls_incomplete",
    )
    for name, field in (
        ("provider_absence", "native_assets"),
        ("database_absence", "database_rows"),
    ):
        absence = observed[name]
        require(
            absence.get("probe_id") == fixture.probe_id
            and absence.get("tenant") == fixture.foreign_tenant
            and absence.get("product") == fixture.foreign_product
            and absence.get(field) == [],
            "scope_foreign_effect_found",
        )
    budget = observed["budget"]
    attempts = budget.get("attempts")
    require(
        isinstance(attempts, list) and 1 <= len(attempts) <= fixture.max_attempts,
        "scope_attempts_invalid",
    )
    attempt_times = []
    for attempt in attempts:
        require(
            isinstance(attempt, Mapping)
            and text(attempt.get("evidence_ref"))
            and attempt["evidence_ref"] not in references,
            "scope_attempt_reference_invalid",
        )
        references.add(attempt["evidence_ref"])
        attempt_times.append(
            timestamp(attempt.get("observed_at"), now, "scope_attempt_time_invalid")
        )
    spent = budget.get("spent_usd")
    require(
        money(spent) and Decimal(spent) <= Decimal(fixture.max_spend_usd),
        "scope_spend_exceeded",
    )
    started = timestamp(budget.get("started_at"), now, "scope_deadline_invalid")
    finished = timestamp(
        observed["cleanup"].get("observed_at"), now, "scope_deadline_invalid"
    )
    require(
        0 <= (finished - started).total_seconds() <= fixture.deadline_seconds
        and all(
            started <= checked <= finished for checked in call_times + attempt_times
        )
        and all(
            timestamp(
                observed[name].get("observed_at"), now, "scope_readback_time_invalid"
            )
            >= max(call_times)
            for name in (
                "policies",
                "grants",
                "provider_absence",
                "database_absence",
                "cleanup",
            )
        ),
        "scope_deadline_exceeded",
    )
    resources = observed["cleanup"].get("resources")
    require(
        isinstance(resources, list) and len(resources) == len(fixture.owned_resources),
        "scope_cleanup_incomplete",
    )
    require(
        all(
            isinstance(item, Mapping)
            and text(item.get("id"))
            and item["id"] in fixture.owned_resources
            and (
                item.get("state") == "absent"
                or (item.get("state") == "retained" and text(item.get("reason")))
            )
            for item in resources
        ),
        "scope_cleanup_unverified",
    )
    require(
        {item["id"] for item in resources} == set(fixture.owned_resources),
        "scope_cleanup_incomplete",
    )
