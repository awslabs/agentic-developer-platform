"""Bounded, non-secret workflow fixtures; never deployment/config overrides."""

import json
import math
import re

from .config import ConfigError, no_secrets, require
from .report import redact

IDENTITY = {"login_user_id", "canonical_user_id", "tenant_id"}
PAID = {"enrollment_verified", "shared_budget_authorized", "max_task_usd"}
KEYS = {
    "human_task_coding": IDENTITY
    | PAID
    | {"max_dispatches", "scenario", "persona", "snapshot", "instructions"},
    "human_task_chat": IDENTITY | PAID | {"max_tasks"},
    "vault_lifecycle": IDENTITY | {"owned_mutations_authorized"},
    "hierarchy_lifecycle": IDENTITY
    | {
        "owned_mutations_authorized",
        "ordinary_login_user_id",
        "ordinary_canonical_user_id",
        "ordinary_native_tenant",
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
    for key in IDENTITY:
        require(
            isinstance(value.get(key), str)
            and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", value[key]),
            f"{name}.{key} requires an explicit fixture identity",
        )
    if name == "hierarchy_lifecycle":
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
    if name in {"vault_lifecycle", "hierarchy_lifecycle"}:
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
