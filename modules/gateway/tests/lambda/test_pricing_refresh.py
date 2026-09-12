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
    monkeypatch.setattr(module, "load_snapshot", lambda: SimpleNamespace(rates=(old,), required_variants=frozenset((old.variant_key,))))
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
    assert rows == () and len(failures) == 9


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
