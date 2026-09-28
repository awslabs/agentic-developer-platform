"""Refresh errors must participate in Lambda asynchronous retry semantics."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pricing_policy.refresh import assemble_candidate
from tests.pricing_policy.test_aws_refresh import VERIFIED, template

from ._handler_loader import load_handler

pytest.importorskip("psycopg2")


@pytest.fixture
def refresh(monkeypatch):
    module = load_handler("pricing-refresh")
    old = template()
    fresh = replace(old, source="model_card", verified_at=VERIFIED, snapshot_version=None)
    candidate = assemble_candidate((old,), (fresh,), frozenset((old.variant_key,)))
    commits = []

    @contextmanager
    def connection():
        yield object()
        commits.append("committed")

    monkeypatch.setattr(module, "get_db_connection", connection)
    monkeypatch.setattr(module, "emit_metrics", Mock())
    monkeypatch.setattr(module, "read_active", Mock(return_value=SimpleNamespace(revision=1, rows=(old,))))
    monkeypatch.setattr(module, "load_snapshot", lambda: SimpleNamespace(models={}, rates=(old,), required_variants=frozenset((old.variant_key,))))
    monkeypatch.setattr(module, "fetch_rates", Mock(return_value=((fresh,), ())))
    monkeypatch.setattr(module, "publish", Mock(return_value=(2, 2, candidate)))
    return module, commits


def test_full_success_publishes_heartbeat(refresh):
    module, commits = refresh
    assert module.handler({}, None)["status"] == "published"
    assert len(commits) == 2
    assert module.emit_metrics.call_args.args[0]["PricingRefreshSuccess"] == 1


def test_partial_commits_before_raising_and_never_emits_success(refresh):
    module, commits = refresh
    module.fetch_rates.return_value = module.fetch_rates.return_value[0], ("https://failed-source",)
    with pytest.raises(module.PartialRefreshError):
        module.handler({}, None)
    assert len(commits) == 2
    assert module.emit_metrics.call_args.args[0]["PricingRefreshPartial"] == 1
    assert "PricingRefreshSuccess" not in module.emit_metrics.call_args.args[0]


@pytest.mark.parametrize("reason", ["schema_absent", "paused", "consumers_disabled_or_unseeded"])
def test_schema_or_paused_defers_without_fetch_or_write(refresh, reason):
    module, _ = refresh
    module.read_active.side_effect = module.RefreshDeferredError(reason)
    assert module.handler({}, None) == {"status": "deferred", "reason": reason}
    module.fetch_rates.assert_not_called()
    module.publish.assert_not_called()


def test_operational_failure_raises_not_http_500(refresh):
    module, _ = refresh
    module.fetch_rates.side_effect = module.SourceValidationError("bad AWS schema")
    with pytest.raises(module.SourceValidationError):
        module.handler({}, None)
    module.publish.assert_not_called()
    assert module.emit_metrics.call_args.args[0] == {"PricingRefreshRejected": 1}


def test_conflict_reloads_and_rebuilds_against_winner(refresh):
    module, _ = refresh
    result = module.publish.return_value
    module.publish.side_effect = [module.PointerConflictError(), result]
    module.read_active.side_effect = [SimpleNamespace(revision=1, rows=(template(),)), SimpleNamespace(revision=2, rows=(template(),))]
    assert module.handler({}, None)["status"] == "published"
    assert module.publish.call_args.args[1] == 2


def test_caller_cannot_shrink_manifest(refresh):
    module, _ = refresh
    with pytest.raises(ValueError):
        module.handler({"required_variants": []}, None)
    module.fetch_rates.assert_not_called()


def test_all_transport_failures_return_no_fresh_values(refresh, monkeypatch):
    module, _ = refresh
    monkeypatch.undo()
    monkeypatch.setattr(module, "fetch_source", Mock(side_effect=OSError("network unreachable")))
    rows, failures = module.fetch_rates((template("openai.gpt-oss-120b", context="flat"),), module.time.monotonic() + 2)
    assert rows == () and len(failures) == len(module.CARD_SLUGS) + 1


def test_unparseable_fetched_source_is_rejected(refresh, monkeypatch):
    module, _ = refresh
    monkeypatch.undo()
    monkeypatch.setattr(module, "fetch_source", Mock(return_value=b"unexpected document"))
    with pytest.raises(module.SourceValidationError):
        module.fetch_rates((template(),), module.time.monotonic() + 2)


def test_source_age_metric_uses_oldest_retained_row(refresh, monkeypatch):
    module, _ = refresh
    monkeypatch.undo()
    client = Mock()
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: client)
    first = replace(template(), verified_at="2026-09-01T00:00:00+00:00")
    second = replace(first, region="us-east-2", verified_at="2026-09-10T00:00:00+00:00")
    module.emit_metrics({"PricingRefreshPartial": 1}, rows=(first, second))
    metrics = client.put_metric_data.call_args.kwargs["MetricData"]
    source = next(item for item in metrics if item["MetricName"] == "PricingSourceVerifiedAgeHours")
    oldest = next(item for item in metrics if item["MetricName"] == "PricingOldestVerifiedAgeHours")
    assert source["Value"] == oldest["Value"]
    assert {item["Name"] for item in source["Dimensions"]} == {"FunctionName", "ModelId", "Source"}
    assert source["Value"] >= 11 * 24


def test_gzip_source_is_decoded_and_bounded(refresh, monkeypatch):
    import gzip
    import io

    module, _ = refresh
    monkeypatch.undo()

    class Response(io.BytesIO):
        url = "https://b0.p.awsstatic.com/source"

    payload = b'{"manifest":"real gzip transport"}'
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: Response(gzip.compress(payload)))
    assert module.fetch_source(Response.url, 1024, module.time.monotonic() + 2) == payload
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: Response(gzip.compress(b"x" * 10000)))
    with pytest.raises(module.SourceValidationError, match="expanded source"):
        module.fetch_source(Response.url, 1024, module.time.monotonic() + 2)
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: Response(gzip.compress(payload)[:-4]))
    with pytest.raises(module.SourceValidationError, match="invalid gzip"):
        module.fetch_source(Response.url, 1024, module.time.monotonic() + 2)


def test_coordinated_claude_sources_refresh_all_rates_and_partial_retains(refresh, monkeypatch):
    from pathlib import Path

    from pricing_policy.aws_sources import CARD_BASE, CARD_SLUGS, CATALOG_BASE
    from pricing_policy.claude_sources import PRICING_PAGE_URL, TOKEN_MAP_URL
    from pricing_policy.policy import load_snapshot

    module, _ = refresh
    monkeypatch.undo()
    fixtures = Path(__file__).parents[1] / "pricing_policy" / "fixtures" / "aws"
    snapshot = load_snapshot("2026-09-12.2")
    monkeypatch.setattr(module, "CARD_SLUGS", {model: slug for model, slug in CARD_SLUGS.items() if model in snapshot.models})
    sources = {CARD_BASE + slug + ".md": (fixtures / (slug + ".md")).read_bytes() for slug in CARD_SLUGS.values()}
    sources[CATALOG_BASE + "/us-east-1/index.json"] = (fixtures / "oss-us-east-1.json").read_bytes()
    sources[PRICING_PAGE_URL] = (fixtures / "claude" / "pricing-widgets.html").read_bytes()
    sources[TOKEN_MAP_URL] = (fixtures / "claude" / "token-map.json").read_bytes()
    monkeypatch.setattr(module, "fetch_source", lambda url, *args: sources[url])
    rows, failures = module.fetch_rates(snapshot.rates, module.time.monotonic() + 20, snapshot.models)
    assert len(rows) == 1336 and not failures
    assert {r.variant_key for r in rows} == snapshot.required_variants

    def missing_map(url, *args):
        if url == TOKEN_MAP_URL:
            raise OSError("unavailable map")
        return sources[url]

    monkeypatch.setattr(module, "fetch_source", missing_map)
    rows, failures = module.fetch_rates(snapshot.rates, module.time.monotonic() + 20, snapshot.models)
    assert len(rows) == 330 and failures == (TOKEN_MAP_URL,)
    candidate = assemble_candidate(snapshot.rates, rows, snapshot.required_variants)
    assert len(candidate.retained_keys) == 1006
    assert all(r.model_id.startswith("openai.") for r in rows)


def test_gpt6_sol_luna_refresh_includes_cache_rates(refresh, monkeypatch):
    from pathlib import Path

    from pricing_policy import load_snapshot
    from pricing_policy.aws_sources import CARD_BASE, CARD_SLUGS

    module, _ = refresh
    monkeypatch.undo()
    fixtures = Path(__file__).parents[1] / "pricing_policy" / "fixtures" / "aws"
    cards = {model: slug for model, slug in CARD_SLUGS.items() if model in {"openai.gpt-6-sol", "openai.gpt-6-luna"}}
    monkeypatch.setattr(module, "CARD_SLUGS", cards)
    sources = {CARD_BASE + slug + ".md": (fixtures / (slug + ".md")).read_bytes() for slug in cards.values()}
    monkeypatch.setattr(module, "fetch_source", lambda url, *args: sources[url])
    snapshot = load_snapshot()
    templates = tuple(row for row in snapshot.rates if row.model_id in cards)
    rows, failures = module.fetch_rates(templates, module.time.monotonic() + 20, snapshot.models)
    assert len(rows) == 152 and not failures
    assert {row.variant_key for row in rows} == {row.variant_key for row in templates}
    assert all(row.cache_read_price_per_1k_tokens == row.input_price_per_1k_tokens / 10 for row in rows)


def test_claude_source_metric_classification(refresh, monkeypatch):
    from pricing_policy.policy import load_snapshot

    module, _ = refresh
    monkeypatch.undo()
    client = Mock()
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: client)
    row = next(r for r in load_snapshot("2026-09-12.2").rates if r.model_id.startswith("anthropic."))
    module.emit_metrics({"PricingRefreshSuccess": 1}, rows=(row,))
    metric = next(v for v in client.put_metric_data.call_args.kwargs["MetricData"] if v["MetricName"] == "PricingSourceVerifiedAgeHours")
    assert {"Name": "Source", "Value": "pricing_page"} in metric["Dimensions"]


def test_manual_partial_report_preserves_partial_metrics(refresh):
    module, commits = refresh
    module.fetch_rates.return_value = module.fetch_rates.return_value[0], ("https://failed-source",)
    result = module.handler({"report_partial": True}, None)
    assert result["partial"] is True
    assert result["failed_sources"] == ["https://failed-source"]
    assert result["fresh_variants"] == 1
    assert len(commits) == 2
    assert module.emit_metrics.call_args.args[0]["PricingRefreshPartial"] == 1
    assert "PricingRefreshSuccess" not in module.emit_metrics.call_args.args[0]


@pytest.mark.parametrize("value", ["false", 1, None])
def test_partial_reporting_flag_is_strict(refresh, value):
    module, _ = refresh
    with pytest.raises(ValueError, match="boolean"):
        module.handler({"report_partial": value}, None)
    module.publish.assert_not_called()
