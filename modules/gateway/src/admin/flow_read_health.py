"""Process-local aggregate diagnostics; never expose tenant or cache identities."""

from src.admin.request_log_queue import BATCH_RECORDS
from src.admin.request_log_queue import EVENTS as QUEUE_EVENTS
from src.orchestration.read_cache import EVENTS as CACHE_EVENTS


def get_flow_read_health() -> dict[str, float]:
    result = {}
    for prefix, metric in (("request_log_queue", QUEUE_EVENTS), ("flow_read_cache", CACHE_EVENTS)):
        for family in metric.collect():
            for sample in family.samples:
                if sample.name.endswith("_total"):
                    result[f"{prefix}_{sample.labels['outcome']}"] = sample.value
    for family in BATCH_RECORDS.collect():
        for sample in family.samples:
            if sample.name.endswith(("_count", "_sum")):
                result[sample.name.removeprefix("bedrock_gateway_")] = sample.value
    return result
