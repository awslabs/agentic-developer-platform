"""Assess controlled access evidence without promoting it to live acceptance."""

import re

from tests.e2e.tools.atlassian_contract import METHODS


CLIENTS = frozenset({"claude-code", "codex"})
DENIALS = frozenset({"read_only", "foreign_owner", "foreign_site", "expired", "disconnected", "policy_denied"})
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _digest(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _denials(client, rows):
    if type(rows) is not dict or set(rows) != DENIALS:
        return False
    for variant, row in rows.items():
        if (type(row) is not dict or set(row) != {
                "client", "variant", "source", "provider_source", "result", "client_data", "receipt",
                "provider_effects_before", "provider_effects_after", "provider_content_before", "provider_content_after"
        } or row["client"] != client or row["variant"] != variant
                or row["source"] != "installed-client-cli-mcp" or row["provider_source"] != "direct-atlassian-provider"
                or row["result"] != "denied" or row["client_data"] is not None or row["receipt"] is not None
                or type(row["provider_effects_before"]) is not int or row["provider_effects_before"] < 0
                or type(row["provider_effects_after"]) is not int
                or row["provider_effects_after"] != row["provider_effects_before"]
                or not _digest(row["provider_content_before"])
                or row["provider_content_after"] != row["provider_content_before"]):
            return False
    return True


def _refresh(client, row):
    return (type(row) is dict and set(row) == {
        "client", "source", "provider_source", "before_epoch", "expires_epoch", "after_epoch",
        "token_reads_before", "token_reads_after", "consent_before", "consent_after",
        "adp_refresh_calls", "adp_token_scan", "provider_account_before", "provider_account_after", "readback_after"
    } and row["client"] == client and row["source"] == "agentcore-identity"
        and row["provider_source"] == "direct-atlassian-provider"
        and all(type(row[key]) is int and row[key] >= 0 for key in (
            "before_epoch", "expires_epoch", "after_epoch", "token_reads_before", "token_reads_after",
            "consent_before", "consent_after", "adp_refresh_calls"))
        and row["before_epoch"] < row["expires_epoch"] <= row["after_epoch"]
        and row["token_reads_after"] > row["token_reads_before"]
        and row["consent_before"] >= 1 and row["consent_after"] == row["consent_before"]
        and row["adp_refresh_calls"] == 0 and row["adp_token_scan"] == "absent"
        and _digest(row["provider_account_before"])
        and row["provider_account_after"] == row["provider_account_before"]
        and row["readback_after"] == "confirmed")


def _schema_set(schemas):
    return (type(schemas) is dict and set(METHODS.values()) <= set(schemas) and len(schemas) <= 1024
            and all(isinstance(name, str) and 0 < len(name) <= 128 and _digest(value)
                    for name, value in schemas.items()))


def _catalogue(client, row, schemas, now_epoch):
    if (not _schema_set(schemas) or type(now_epoch) is not int or type(row) is not dict
            or set(row) != {"client", "source", "producer", "negative", "replicas", "loading"}
            or row["client"] != client or row["source"] != "installed-client-cli-mcp"):
        return False
    producer, negative, replicas, loading = (row[key] for key in ("producer", "negative", "replicas", "loading"))
    if (type(producer) is not dict or set(producer) != {
            "source", "observed_at", "expires_at", "schemas", "provenance"
    } or producer["source"] != "catalogue-observation" or producer["provenance"] != "verified"
            or type(producer["observed_at"]) is not int or type(producer["expires_at"]) is not int
            or not producer["observed_at"] <= now_epoch < producer["expires_at"]
            or not 0 < producer["expires_at"] - producer["observed_at"] <= 60
            or producer["schemas"] != schemas
            or negative != {"expired": "unavailable", "invalid_provenance": "unavailable"}
            or type(replicas) is not dict or not 2 <= len(replicas) <= 4):
        return False
    if any(not isinstance(replica, str) or not replica or type(observation) is not dict
           or set(observation) != {"source", "cached_miss", "fresh", "next_session", "forced_cache_clear"}
           or observation["source"] != "normal-installed-cli-mcp" or observation["cached_miss"] != {}
           or observation["fresh"] != schemas or observation["next_session"] != schemas
           or observation["forced_cache_clear"] is not False for replica, observation in replicas.items()):
        return False
    return (type(loading) is dict and set(loading) == {
        "mode", "reported_tool_count", "observed_tool_count", "named_tool", "named_call"
    } and loading["mode"] == "bounded-eager-named-tools"
        and type(loading["reported_tool_count"]) is int and loading["reported_tool_count"] == len(schemas)
        and type(loading["observed_tool_count"]) is int and loading["observed_tool_count"] == len(schemas)
        and isinstance(loading["named_tool"], str) and loading["named_tool"] in schemas
        and loading["named_call"] == "provider-confirmed")


def assess_authority(client, *, denials, identity, catalogue, independent_schemas, now_epoch):
    if client not in CLIENTS:
        raise ValueError("A distinct installed client is required")
    results = {"denial": _denials(client, denials), "identity_refresh": _refresh(client, identity),
               "catalogue_schema": _catalogue(client, catalogue, independent_schemas, now_epoch)}
    return {"client": client, "controlled": "candidate" if all(results.values()) else "blocked",
            "live": "unverified", "checks": {key: "candidate" if valid else "blocked"
                                             for key, valid in results.items()}}
