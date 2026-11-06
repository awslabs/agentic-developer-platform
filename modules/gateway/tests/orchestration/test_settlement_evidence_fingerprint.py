"""Complete source provenance for an unchanged settlement approval."""

import json
from copy import deepcopy
from dataclasses import asdict

import httpx
import pytest

from src.orchestration.merge_evidence import SEMANTIC_SOURCE_KINDS, EvidenceUnavailableError
from src.orchestration.protected_merge_recovery import snapshot_verification
from src.orchestration.review_cycle import CycleBlockedError
from tests.orchestration.test_merge_evidence import observe, provider_data, unavailable_rules_data

SINGULAR = ("repository_settings", "pull_request", "pull_request_recheck", "base_branch", "base_branch_recheck", "review_requirements")
PAGED = ("branch_rules", "check_runs", "statuses", "reviews")


def complete_sources():
    return [{"kind": kind, "digest": "a" * 64, "semantic_digest": "b" * 64} for kind in (*SINGULAR, *PAGED, "branch_protection")]


@pytest.mark.parametrize("kind", sorted(SEMANTIC_SOURCE_KINDS))
@pytest.mark.parametrize("defect", ["missing_digest", "malformed_digest", "missing_raw_digest", "malformed_raw_digest"])
def test_settlement_requires_valid_raw_and_semantic_provenance(kind, defect):
    sources = complete_sources()
    if kind == "rules_capability_verification":
        sources[-1]["kind"] = kind
    source = next(item for item in sources if item["kind"] == kind)
    field = "digest" if "raw" in defect else "semantic_digest"
    if defect.startswith("missing"):
        source.pop(field)
    else:
        source[field] = "invalid"
    with pytest.raises(CycleBlockedError, match="merge_evidence_unavailable"):
        snapshot_verification({"check_sources": sources})


@pytest.mark.parametrize("kind", (*SINGULAR, *PAGED, "branch_protection"))
def test_settlement_requires_every_consumed_source(kind):
    sources = [source for source in complete_sources() if source["kind"] != kind]
    with pytest.raises(CycleBlockedError, match="merge_evidence_unavailable"):
        snapshot_verification({"check_sources": sources})


@pytest.mark.parametrize("kind", (*SINGULAR, "branch_protection", "rules_capability_verification"))
def test_settlement_refuses_duplicate_nonpaginated_source(kind):
    sources = complete_sources()
    if kind == "rules_capability_verification":
        sources[-1]["kind"] = kind
    sources.append(dict(next(source for source in sources if source["kind"] == kind)))
    with pytest.raises(CycleBlockedError, match="merge_evidence_unavailable"):
        snapshot_verification({"check_sources": sources})


@pytest.mark.parametrize("source", [{"kind": "unknown_security_source"}, {"kind": None}, {}, None])
def test_unknown_or_malformed_source_refuses_even_with_semantic_digest(source):
    if isinstance(source, dict):
        source = {**source, "digest": "a" * 64, "semantic_digest": "b" * 64}
    with pytest.raises(CycleBlockedError, match="merge_evidence_unavailable"):
        snapshot_verification({"check_sources": [*complete_sources(), source]})


def test_semantic_snapshot_keeps_paged_evidence_and_does_not_mutate_audit():
    sources = complete_sources()
    sources.extend({"kind": kind, "digest": "c" * 64, "semantic_digest": "d" * 64} for kind in PAGED)
    raw = {"check_sources": sources}
    before = deepcopy(raw)
    projected = snapshot_verification(raw)
    assert raw == before
    assert all(source["digest"] == source["semantic_digest"] for source in projected["check_sources"])
    assert len(projected["check_sources"]) == len(sources)
    # Capability verification may replace unavailable protection or accompany
    # a recorded paid-plan denial from the protection endpoint.
    sources.append({"kind": "rules_capability_verification", "digest": "e" * 64, "semantic_digest": "f" * 64})
    assert snapshot_verification(raw)


def reordered(value):
    if isinstance(value, dict):
        return {key: reordered(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [reordered(item) for item in value]
    return value


def verification(observed):
    return {
        "check_sources": [
            {"kind": source.kind, "digest": source.payload_sha256, "semantic_digest": source.semantic_sha256} for source in observed.sources
        ]
    }


@pytest.mark.parametrize(
    "key", ["pr", "branch", "rules", "protection", "repository", "checks", "statuses", "reviews", "graphql", "capability", "denied_rules"]
)
async def test_provider_json_serialization_is_neutral_to_settlement_but_retained_in_raw_audit(key):
    data = unavailable_rules_data() if key in {"capability", "denied_rules"} else provider_data()
    before, _ = await observe(data)
    key = "rules" if key == "denied_rules" else key
    value = data[key]
    status = value.status_code if isinstance(value, httpx.Response) else 404 if value is None else 200
    payload = value.json() if isinstance(value, httpx.Response) else value
    encoded = json.dumps(reordered(payload), indent=1).encode() + b"\n"
    assert json.loads(encoded) == payload
    data[key] = httpx.Response(status, content=encoded)
    after, _ = await observe(data)
    assert {k: v for k, v in asdict(before).items() if k != "sources"} == {k: v for k, v in asdict(after).items() if k != "sources"}
    old, new = verification(before), verification(after)
    assert old != new
    assert snapshot_verification(old) == snapshot_verification(new)


@pytest.mark.parametrize("kind", sorted(SEMANTIC_SOURCE_KINDS - {"repository_settings", "pull_request", "pull_request_recheck"}))
async def test_canonical_sources_preserve_every_unknown_json_field(kind):
    # Exercise the production decoder, not a hand-written hash. Except for the
    # two explicitly reviewed projections, all fields remain security evidence.
    from datetime import UTC, datetime

    from src.orchestration.merge_evidence import GitHubMergeObserver

    observer = GitHubMergeObserver(None, "scoped-test-token", lambda: datetime.now(UTC))
    before = {"proof": {"head": "a" * 40, "future_security_field": {"enabled": True}}}
    after = deepcopy(before)
    after["proof"]["future_security_field"]["enabled"] = False
    for payload in (before, after):
        observer._decode(httpx.Response(200, json=payload), kind=kind, url="https://api.github.com/test")
    old, new = observer.sources
    assert old.payload_sha256 != new.payload_sha256 and old.semantic_sha256 != new.semantic_sha256


@pytest.mark.parametrize("body", [b"", b"not-json", b" " * (4 * 1024 * 1024 + 1)], ids=["empty", "non-json", "oversized"])
async def test_malformed_absence_proof_refuses(body):
    data = provider_data()
    data["protection"] = httpx.Response(404, content=body)
    with pytest.raises(EvidenceUnavailableError):
        await observe(data)
