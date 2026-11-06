"""Controlled authority assessment; gateway cache tests exercise the producer itself."""

from copy import deepcopy

import pytest

from tests.e2e.tools.atlassian_authority import DENIALS, assess_authority
from tests.e2e.tools.atlassian_contract import METHODS


def evidence(client):
    schemas = {method: "a" * 64 for method in METHODS.values()}
    schemas["new-provider-tool"] = "b" * 64
    denials = {variant: {"source": "installed-client-cli-mcp", "provider_source": "direct-atlassian-provider",
                         "client": client, "variant": variant, "result": "denied", "client_data": None,
                         "receipt": None, "provider_effects_before": 0, "provider_effects_after": 0,
                         "provider_content_before": "c" * 64, "provider_content_after": "c" * 64}
               for variant in DENIALS}
    identity = {"client": client, "source": "agentcore-identity", "provider_source": "direct-atlassian-provider",
                "before_epoch": 10, "expires_epoch": 11, "after_epoch": 12,
                "token_reads_before": 1, "token_reads_after": 2,
                "consent_before": 1, "consent_after": 1, "adp_refresh_calls": 0,
                "adp_token_scan": "absent", "provider_account_before": "d" * 64,
                "provider_account_after": "d" * 64, "readback_after": "confirmed"}
    catalogue = {"client": client, "source": "installed-client-cli-mcp",
                 "producer": {"source": "catalogue-observation", "observed_at": 10, "expires_at": 60,
                              "schemas": schemas, "provenance": "verified"},
                 "negative": {"expired": "unavailable", "invalid_provenance": "unavailable"},
                 "replicas": {replica: {"source": "normal-installed-cli-mcp", "cached_miss": {},
                                         "fresh": schemas, "next_session": schemas, "forced_cache_clear": False}
                              for replica in ("replica-a", "replica-b")},
                 "loading": {"mode": "bounded-eager-named-tools", "reported_tool_count": len(schemas),
                             "observed_tool_count": len(schemas), "named_tool": "new-provider-tool",
                             "named_call": "provider-confirmed"}}
    return {"denials": denials, "identity": identity, "catalogue": catalogue,
            "independent_schemas": schemas, "now_epoch": 12}


@pytest.mark.parametrize("client", ["claude-code", "codex"])
def test_controlled_authority_keeps_denial_refresh_and_catalogue_separate(client):
    report = assess_authority(client, **evidence(client))
    assert report == {"client": client, "controlled": "candidate", "live": "unverified",
                      "checks": {"denial": "candidate", "identity_refresh": "candidate",
                                 "catalogue_schema": "candidate"}}
    with pytest.raises(ValueError, match="distinct installed client"):
        assess_authority("direct-adapter", **evidence(client))


@pytest.mark.parametrize("variant", sorted(DENIALS))
@pytest.mark.parametrize("change", [{"result": "allowed"}, {"provider_effects_after": 1}, {"provider_effects_after": True},
                                      {"client_data": "denied-data"}, {"provider_content_after": "f" * 64}])
def test_denial_effects_and_data_must_be_absent(variant, change):
    observations = evidence("codex")
    observations["denials"][variant].update(change)
    report = assess_authority("codex", **observations)
    assert report["controlled"] == "blocked" and report["live"] == "unverified"
    assert report["checks"] == {"denial": "blocked", "identity_refresh": "candidate",
                                 "catalogue_schema": "candidate"}


@pytest.mark.parametrize("field,value", [
    ("after_epoch", 10), ("token_reads_after", 1), ("consent_after", 2),
    ("adp_refresh_calls", 1), ("adp_token_scan", "present"),
    ("provider_account_after", "f" * 64), ("source", "adp-direct-refresh")
])
def test_expiry_requires_identity_managed_refresh_without_reconsent_or_token_storage(field, value):
    observations = evidence("claude-code")
    observations["identity"][field] = value
    report = assess_authority("claude-code", **observations)
    assert report["checks"] == {"denial": "candidate", "identity_refresh": "blocked",
                                 "catalogue_schema": "candidate"}


@pytest.mark.parametrize("change", [
    ("producer", "expires_at", 12), ("producer", "provenance", "invalid"),
    ("negative", "expired", "ready"), ("negative", "invalid_provenance", "ready"),
    ("replica-a", "fresh", {}), ("replica-b", "next_session", {}),
    ("replica-a", "source", "direct-door"), ("replica-b", "forced_cache_clear", True),
    ("loading", "observed_tool_count", 1), ("loading", "named_call", "skipped"), ("loading", "named_tool", [])
])
def test_catalogue_expiry_provenance_and_cached_miss_recovery_require_normal_clients(change):
    observations = evidence("codex")
    section, field, value = change
    if section.startswith("replica-"):
        observations["catalogue"]["replicas"][section][field] = value
    else:
        observations["catalogue"][section][field] = value
    report = assess_authority("codex", **observations)
    assert report["controlled"] == "blocked" and report["live"] == "unverified"
    assert report["checks"] == {"denial": "candidate", "identity_refresh": "candidate",
                                 "catalogue_schema": "blocked"}


def test_independent_schema_set_cannot_be_replaced_by_a_producer_or_second_client_claim():
    observations = evidence("codex")
    independent = deepcopy(observations["independent_schemas"])
    independent["new-provider-tool"] = "f" * 64
    observations["independent_schemas"] = independent
    assert assess_authority("codex", **observations)["checks"]["catalogue_schema"] == "blocked"
    observations = evidence("codex")
    observations["catalogue"]["replicas"].pop("replica-b")
    assert assess_authority("codex", **observations)["checks"]["catalogue_schema"] == "blocked"
