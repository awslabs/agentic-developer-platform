"""Bounded, non-secret workflow fixtures; never deployment/config overrides."""

import json
import math
import re

from .config import ConfigError, no_secrets, require
from .report import redact

IDENTITY = {"login_user_id", "canonical_user_id", "tenant_id"}
PAID = {"enrollment_verified", "shared_budget_authorized", "max_task_usd"}
CONTRAST_KEYS = {
    "disabled_feature",
    "enabled_feature",
    "disabled_operation",
    "enabled_operation",
    "denied_operation",
    "foreign_request_id",
    "ordinary_fixture_name",
}
KEYS = {
    "tenant_isolation": {"tenant_ids"},
    "capability_contrast": CONTRAST_KEYS,
    "human_task_coding": IDENTITY
    | PAID
    | {
        "max_dispatches",
        "scenario",
        "persona",
        "snapshot",
        "instructions",
        "control_when",
        "running_wait_seconds",
        "require_activity_list",
    },
    "human_task_chat": IDENTITY | PAID | {"max_tasks"},
    "knowledge_lifecycle": IDENTITY
    | {
        "bucket",
        "owned_mutations_authorized",
        "source_upload_verified",
        "runtime_cost_verified",
        "max_attempts",
        "max_spend_usd",
        "verified_worst_case_usd",
        "cost_evidence_sha256",
        "source_etag",
        "source_version_id",
    },
    "budget_lifecycle": IDENTITY
    | {
        "owned_mutations_authorized",
        "ordinary_canonical_user_id",
        "exclusive_ordinary_fixture",
    },
    "machine_lifecycle": IDENTITY
    | {
        "cognito_lifecycle",
        "owned_mutations_authorized",
        "ordinary_canonical_user_id",
        "ordinary_native_tenant",
    },
    "vault_lifecycle": IDENTITY | {"owned_mutations_authorized"},
    "hierarchy_lifecycle": IDENTITY
    | {
        "owned_mutations_authorized",
        "ordinary_login_user_id",
        "ordinary_canonical_user_id",
        "ordinary_native_tenant",
        "role_transition",
        "role_scope_release",
        "exclusive_ordinary_fixture",
    },
}


def validate_fixture(name, value):
    require(
        name in KEYS and isinstance(value, dict),
        "Unknown fixture or non-object fixture",
    )
    require(set(value) <= KEYS[name], f"Unknown {name} fixture keys")
    no_secrets(value, name)
    require(redact(value) == value, f"{name} must contain no credential material")
    if name == "tenant_isolation":
        tenants = value.get("tenant_ids")
        require(
            isinstance(tenants, list)
            and len(tenants) == 2
            and all(
                isinstance(tenant, str)
                and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", tenant)
                for tenant in tenants
            )
            and len(set(tenants)) == 2,
            "Two distinct explicit existing tenant IDs required",
        )
        return
    if name == "capability_contrast":
        require(
            set(value) == CONTRAST_KEYS, "Complete capability contrast fixture required"
        )
        for key in CONTRAST_KEYS:
            require(
                isinstance(value[key], str) and 0 < len(value[key]) <= 128,
                "Invalid capability contrast value",
            )
        return
    for key in IDENTITY:
        require(
            isinstance(value.get(key), str)
            and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", value[key]),
            f"{name}.{key} requires an explicit fixture identity",
        )
    if name == "knowledge_lifecycle":
        from .remote.knowledge_lifecycle_plan import validate_dispatch_fixture

        try:
            validate_dispatch_fixture(value)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        require(
            isinstance(value.get("bucket"), str)
            and re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value["bucket"]),
            "Exact knowledge source bucket required",
        )
        return
    if name == "budget_lifecycle":
        require(
            value.get("exclusive_ordinary_fixture") is True,
            "Exclusive ordinary fixture required",
        )
        require(
            isinstance(value.get("ordinary_canonical_user_id"), str)
            and re.fullmatch(
                r"[A-Za-z0-9_.:@-]{1,128}", value["ordinary_canonical_user_id"]
            )
            and value["ordinary_canonical_user_id"] != value["canonical_user_id"],
            "Independent ordinary fixture identity required",
        )
    if name == "machine_lifecycle":
        require(
            type(value.get("cognito_lifecycle", False)) is bool,
            "Cognito lifecycle selection must be boolean",
        )
        for key in ("ordinary_canonical_user_id", "ordinary_native_tenant"):
            require(
                isinstance(value.get(key), str)
                and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", value[key]),
                "Explicit ordinary fixture required",
            )
        require(
            value["ordinary_canonical_user_id"] != value["canonical_user_id"]
            and value["ordinary_native_tenant"] != value["tenant_id"],
            "Independent ordinary and native tenant required",
        )
    if name == "hierarchy_lifecycle":
        require(
            type(value.get("role_transition", False)) is bool,
            "Role transition selection must be boolean",
        )
        if value.get("role_transition") is True:
            require(
                value.get("exclusive_ordinary_fixture") is True,
                "Role transition requires exclusive ordinary fixture",
            )
            require(
                isinstance(value.get("role_scope_release"), str)
                and re.fullmatch(r"[0-9a-f]{40}", value["role_scope_release"]),
                "Exact reviewed department scope release required",
            )
        else:
            require(
                "role_scope_release" not in value,
                "Role scope release requires explicit role transition selection",
            )
        for key in (
            "ordinary_login_user_id",
            "ordinary_canonical_user_id",
            "ordinary_native_tenant",
        ):
            require(
                isinstance(value.get(key), str)
                and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", value[key]),
                "Explicit ordinary fixture identity required",
            )
        require(
            value["ordinary_login_user_id"] != value["login_user_id"],
            "Independent fixture subjects required",
        )
        require(
            value["ordinary_native_tenant"] != value["tenant_id"],
            "Two fixture tenants required",
        )
    if name in {
        "vault_lifecycle",
        "hierarchy_lifecycle",
        "machine_lifecycle",
        "budget_lifecycle",
    }:
        require(
            value.get("owned_mutations_authorized") is True,
            "Owned metadata mutations must be explicitly authorized",
        )
        return
    require(
        all(
            value.get(key) is True
            for key in ("enrollment_verified", "shared_budget_authorized")
        ),
        "Existing enrollment and shared budget authorization required",
    )
    budget = value.get("max_task_usd")
    require(
        type(budget) in (int, float) and math.isfinite(budget) and 0 < budget <= 1,
        "Per-task fixture budget must be in (0,1]",
    )
    if name == "human_task_chat":
        require(
            type(value.get("max_tasks")) is int and value["max_tasks"] == 2,
            "Chat fixture requires exactly two tasks",
        )
        return
    require(
        type(value.get("max_dispatches")) is int and value["max_dispatches"] == 1,
        "Coding fixture requires exactly one dispatch",
    )
    require(value.get("scenario") in {"complete", "cancel"}, "Unknown coding scenario")
    require(
        type(value.get("require_activity_list", False)) is bool,
        "Activity list selection must be boolean",
    )
    require(
        value.get("control_when", "observed") in {"observed", "running"},
        "Unknown coding control timing",
    )
    require(
        type(value.get("running_wait_seconds", 30)) is int
        and 1 <= value.get("running_wait_seconds", 30) <= 180,
        "Running wait must be 1..180 seconds",
    )
    require(
        value.get("control_when") != "running" or value["scenario"] == "cancel",
        "Running coding control supports cancellation only",
    )
    require(
        value.get("persona")
        in {"agent-task-claude-developer", "agent-task-codex-developer"},
        "Unknown coding persona",
    )
    require(
        isinstance(value.get("instructions"), str)
        and 0 < len(json.dumps(value["instructions"]).encode()) <= 4096,
        "Coding instructions exceed the recovery bound",
    )
    snapshot = value.get("snapshot")
    require(
        isinstance(snapshot, dict)
        and set(snapshot)
        == {
            "schema_version",
            "repository_id",
            "repository",
            "commit_sha",
            "issue",
            "files",
        },
        "Coding snapshot must use the exact published schema",
    )
    require(
        snapshot["schema_version"] == "1.0"
        and type(snapshot["repository_id"]) is int
        and snapshot["repository_id"] > 0
        and type(snapshot["issue"]) is int
        and snapshot["issue"] > 0,
        "Invalid repository/issue snapshot identity",
    )
    require(
        isinstance(snapshot["repository"], str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", snapshot["repository"]),
        "Invalid repository name",
    )
    require(
        isinstance(snapshot["commit_sha"], str)
        and re.fullmatch(r"[0-9a-f]{40}", snapshot["commit_sha"]),
        "Snapshot requires immutable commit SHA",
    )
    files = snapshot["files"]
    require(
        isinstance(files, list)
        and 1 <= len(files) <= 32
        and len(json.dumps(snapshot).encode()) <= 262144,
        "Snapshot exceeds file/size bounds",
    )
    paths = set()
    for row in files:
        require(
            isinstance(row, dict) and set(row) == {"path", "blob_sha", "content"},
            "Invalid snapshot file fields",
        )
        path = row["path"]
        require(
            isinstance(path, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", path)
            and not {".", "..", ".git"}.intersection(path.split("/"))
            and path not in paths,
            "Invalid or duplicate snapshot path",
        )
        paths.add(path)
        require(
            isinstance(row["blob_sha"], str)
            and re.fullmatch(r"[0-9a-f]{40}", row["blob_sha"])
            and isinstance(row["content"], str),
            "Invalid snapshot blob/content",
        )


def parse(raw):
    require(len(raw.encode()) <= 300000, "Fixture JSON exceeds bounded input size")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate fixture JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, TypeError):
        raise ConfigError("Fixture input must be valid bounded JSON") from None
    require(
        isinstance(value, dict) and set(value) <= set(KEYS),
        "Fixture input permits only supported owned diagnostic fixtures",
    )
    no_secrets(value, "fixtures")
    for name, fixture in value.items():
        validate_fixture(name, fixture)
    return value
