"""Process-local accounting and audit failure signals, independent of EMF."""

from prometheus_client import Counter

METERING_WRITE_FAILURES = Counter("bedrock_gateway_metering_write_failures_total", "Gateway usage write attempts that failed")
METERING_UNCONFIRMED = Counter("bedrock_gateway_metering_unconfirmed_total", "Gateway usage or pricing observations not confirmed")
AUDIT_WRITE_FAILURES = Counter("bedrock_gateway_audit_write_failures_total", "Admin audit write attempts that failed")


def record_health_event(counter: Counter) -> None:
    """A broken optional counter must not change accounting or audit decisions."""
    try:
        counter.inc()
    except Exception:
        pass


def get_accounting_health() -> dict[str, float]:
    """Return only process-local aggregates; receipt state lives elsewhere."""
    return {
        "metering_write_failures": METERING_WRITE_FAILURES._value.get(),
        "metering_unconfirmed_observations": METERING_UNCONFIRMED._value.get(),
        "audit_write_failures": AUDIT_WRITE_FAILURES._value.get(),
    }
