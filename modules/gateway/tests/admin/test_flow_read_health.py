from prometheus_client import CollectorRegistry, Counter, Histogram

from src.admin import flow_read_health


def test_diagnostics_export_aggregate_outcomes_and_batch_size_only(monkeypatch):
    registry = CollectorRegistry()
    cache = Counter("cache", "test", ["outcome"], registry=registry)
    queue = Counter("queue", "test", ["outcome"], registry=registry)
    batches = Histogram("bedrock_gateway_request_log_batch_records", "test", registry=registry)
    cache.labels("hit").inc(7)
    queue.labels("enqueued").inc(12)
    batches.observe(12)
    monkeypatch.setattr(flow_read_health, "CACHE_EVENTS", cache)
    monkeypatch.setattr(flow_read_health, "QUEUE_EVENTS", queue)
    monkeypatch.setattr(flow_read_health, "BATCH_RECORDS", batches)
    assert flow_read_health.get_flow_read_health() == {
        "flow_read_cache_hit": 7.0,
        "request_log_queue_enqueued": 12.0,
        "request_log_batch_records_count": 1.0,
        "request_log_batch_records_sum": 12.0,
    }
