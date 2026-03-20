"""
Request timing utilities for distributed tracing.

Issue #144: Phase 1 - Response Timing Headers
Issue #144: Phase 3 - Bridge RequestTimings with OTEL spans for X-Ray visibility

Provides a RequestTimings helper class that wraps request.state.timings
(a dict of segment_name -> elapsed_ms). Used to instrument each middleware
and service layer to measure latency at each stage.

When OTEL tracing is enabled, time_segment() also creates an OTEL child span
so the component timing appears as a subsegment in X-Ray traces.

Segments tracked:
1. auth       - JWT validation (Cognito JWKS lookup + token verification)
2. model_resolve - model alias resolution + access check
3. budget_check  - budget enforcement middleware (DB query)
4. ratelimit_check - rate limit enforcement middleware
5. bedrock    - actual Bedrock InvokeModel call (non-streaming)
6. bedrock_ttfb - time to get stream object (streaming)
7. serialize  - response parsing and serialization
8. total      - end-to-end gateway time (excluding CloudFront/ALB hops)
"""

import time
from contextlib import contextmanager

from fastapi import Request


def _get_otel_tracer():
    """Get the OTEL tracer if available, otherwise None."""
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("bedrock-gateway.timing")
        # Check if it's a real tracer (not a no-op proxy)
        provider = trace.get_tracer_provider()
        # If no real provider is set, get_tracer_provider returns a ProxyTracerProvider
        # which still works but produces no-op spans. Check for our TracerProvider.
        from opentelemetry.sdk.trace import TracerProvider

        if isinstance(provider, TracerProvider):
            return tracer
    except ImportError:
        pass
    return None


class RequestTimings:
    """
    Helper class for recording per-segment request timings.

    Wraps a dict stored in request.state.timings and provides:
    - time_segment(name): context manager to measure a code block + create OTEL span
    - record(name, elapsed_ms): manually record a timing
    - to_header(): format as X-Gateway-Timing header string
    - to_dict(): for structured JSON logging
    """

    def __init__(self, timings_dict: dict[str, float] | None = None) -> None:
        self._timings: dict[str, float] = timings_dict if timings_dict is not None else {}

    @contextmanager
    def time_segment(self, name: str):
        """
        Context manager to measure elapsed time for a named segment.

        Also creates an OTEL child span (if tracing is enabled) so the
        segment appears as a subsegment in X-Ray traces.

        Args:
            name: Segment name (e.g., 'auth', 'bedrock', 'budget_check')
        """
        tracer = _get_otel_tracer()
        start = time.monotonic()

        if tracer is not None:
            from opentelemetry import trace

            with tracer.start_as_current_span(
                name,
                kind=trace.SpanKind.INTERNAL,
            ) as span:
                try:
                    yield span
                except Exception as e:
                    span.record_exception(e)
                    raise
                finally:
                    elapsed_ms = (time.monotonic() - start) * 1000
                    self._timings[name] = round(elapsed_ms, 1)
                    span.set_attribute("duration_ms", round(elapsed_ms, 1))
        else:
            try:
                yield None
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000
                self._timings[name] = round(elapsed_ms, 1)

    def record(self, name: str, elapsed_ms: float) -> None:
        """Manually record a timing value for a named segment."""
        self._timings[name] = round(elapsed_ms, 1)

    def to_header(self) -> str:
        """
        Format all recorded timings as an X-Gateway-Timing header string.

        Returns:
            Header string like: "auth=5ms;model_resolve=1ms;bedrock=1847ms;total=1870ms"
        """
        preferred_order = [
            "auth",
            "model_resolve",
            "budget_check",
            "ratelimit_check",
            "bedrock",
            "bedrock_ttfb",
            "serialize",
            "total",
        ]

        parts = []
        seen = set()

        for name in preferred_order:
            if name in self._timings:
                parts.append(f"{name}={self._timings[name]:.0f}ms")
                seen.add(name)

        for name, elapsed_ms in self._timings.items():
            if name not in seen:
                parts.append(f"{name}={elapsed_ms:.0f}ms")

        return ";".join(parts)

    def to_dict(self) -> dict[str, float]:
        """Return timings as a dict for structured JSON logging."""
        return dict(self._timings)

    def get(self, name: str) -> float | None:
        """Get the timing for a specific segment."""
        return self._timings.get(name)

    def __len__(self) -> int:
        return len(self._timings)

    def __contains__(self, name: str) -> bool:
        return name in self._timings

    def __repr__(self) -> str:
        return f"RequestTimings({self._timings})"


def get_timings(request: Request) -> RequestTimings:
    """
    Get or lazily initialize RequestTimings from request.state.

    Args:
        request: FastAPI Request object

    Returns:
        RequestTimings: Wrapper around request.state.timings dict
    """
    if not hasattr(request.state, "timings"):
        request.state.timings = {}

    return RequestTimings(request.state.timings)
