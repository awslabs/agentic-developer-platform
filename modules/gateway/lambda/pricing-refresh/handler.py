"""Daily complete-generation AWS Bedrock rate publication (issue #4969).

Operational failures raise so Lambda asynchronous retries/destination work. A
partial refresh commits a complete fresh+retained generation, then raises; it
never overwrites prior rates with fallbacks or renews retained verification ages.
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from urllib.error import URLError
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))
sys.path.insert(0, os.path.dirname(__file__))
from db import get_db_connection
from publication import PointerConflictError, RefreshDeferredError, publish, read_active

from pricing_policy.aws_sources import CARD_BASE, CARD_SLUGS, CATALOG_BASE, SourceValidationError, parse_catalog, parse_model_card
from pricing_policy.claude_sources import PRICING_PAGE_URL, TOKEN_MAP_URL, parse_claude_pricing
from pricing_policy.kimi_sources import KIMI_CARD_URL, KIMI_MODEL, parse_kimi_card
from pricing_policy.policy import load_snapshot

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
FETCH_SECONDS = 120
# libpq honors this for the existing shared connection helper as well.
os.environ.setdefault("PGCONNECT_TIMEOUT", "5")


class PartialRefreshError(RuntimeError):
    """A complete mixed generation committed, but some sources need retry."""


def emit_metrics(values, *, rows=()):
    now = datetime.now(UTC)
    dimensions = [{"Name": "FunctionName", "Value": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "pricing-refresh")}]
    data = [{"MetricName": name, "Value": value, "Unit": "None", "Dimensions": dimensions, "Timestamp": now} for name, value in values.items()]
    source_ages = {}
    for row in rows:
        age = max(0, (now - datetime.fromisoformat(row.verified_at.replace("Z", "+00:00"))).total_seconds() / 3600)
        source = "pricing_page" if row.model_id.startswith("anthropic.") else ("bulk_catalog" if ".gpt-oss-" in row.model_id else "model_card")
        key = (row.model_id, source)
        source_ages[key] = max(age, source_ages.get(key, 0))
    for (model, source), age in source_ages.items():
        data.append(
            {
                "MetricName": "PricingSourceVerifiedAgeHours",
                "Value": age,
                "Unit": "None",
                "Timestamp": now,
                "Dimensions": dimensions + [{"Name": "ModelId", "Value": model}, {"Name": "Source", "Value": source}],
            }
        )
    if source_ages:
        data.append(
            {
                "MetricName": "PricingOldestVerifiedAgeHours",
                "Value": max(source_ages.values()),
                "Unit": "None",
                "Timestamp": now,
                "Dimensions": dimensions,
            }
        )
    # Failing to publish the heartbeat is itself operational failure, never swallowed.
    boto3.client("cloudwatch", config=Config(connect_timeout=3, read_timeout=3, retries={"total_max_attempts": 2})).put_metric_data(
        Namespace="ADP/Gateway", MetricData=data
    )


def fetch_source(url, limit, deadline):
    """Fixed AWS HTTPS URLs only; bounded size, per-read timeout and total budget."""
    error = None
    for _ in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("pricing source deadline exhausted")
        try:
            request = Request(
                url,
                headers={"User-Agent": "ADP-Bedrock-Pricing/2", "Accept": "text/markdown, application/json, text/html", "Accept-Encoding": "gzip"},
            )
            with urlopen(request, timeout=min(10, remaining)) as response:  # noqa: S310 -- fixed AWS URLs
                if not response.url.startswith("https://"):
                    raise SourceValidationError("AWS source redirected to non-HTTPS")
                parts, length = [], 0
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("pricing source deadline exhausted")
                    chunk = response.read(min(65536, limit + 1 - length))
                    if not chunk:
                        content = b"".join(parts)
                        # AWS's pricing-widget map is gzip encoded. Bound both
                        # wire bytes and expanded bytes, including gzip bombs.
                        if content.startswith(b"\x1f\x8b"):
                            try:
                                with gzip.GzipFile(fileobj=io.BytesIO(content)) as compressed:
                                    content = compressed.read(limit + 1)
                            except (OSError, EOFError, zlib.error) as exc:
                                raise SourceValidationError("invalid gzip AWS pricing source") from exc
                            if len(content) > limit:
                                raise SourceValidationError(f"expanded source exceeds {limit} byte limit: {url}")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("pricing source deadline exhausted")
                        return content
                    parts.append(chunk)
                    length += len(chunk)
                    if length > limit:
                        raise SourceValidationError(f"source exceeds {limit} byte limit: {url}")
        except (URLError, OSError, TimeoutError) as exc:
            error = exc
    raise RuntimeError(f"AWS source transport failure: {url}") from error


def fetch_rates(templates, deadline, models=None):
    jobs = [("card", model, CARD_BASE + slug + ".md", 1024 * 1024) for model, slug in CARD_SLUGS.items()]
    if any(row.model_id == KIMI_MODEL for row in templates):
        jobs.append(("kimi_card", KIMI_MODEL, KIMI_CARD_URL, 1024 * 1024))
    regions = sorted({row.region for row in templates if ".gpt-oss-" in row.model_id})
    jobs.extend(("catalog", region, CATALOG_BASE + "/" + region + "/index.json", 10 * 1024 * 1024) for region in regions)
    if any(row.model_id.startswith("anthropic.") for row in templates):
        jobs.extend((("claude_page", "claude", PRICING_PAGE_URL, 10 * 1024 * 1024), ("claude_map", "claude", TOKEN_MAP_URL, 10 * 1024 * 1024)))
    claude_documents = {}
    fresh, failures = [], []
    pool = ThreadPoolExecutor(max_workers=4)
    futures = {pool.submit(fetch_source, url, limit, deadline): (kind, identity, url) for kind, identity, url, limit in jobs}
    try:
        for future in as_completed(futures, timeout=max(0, deadline - time.monotonic())):
            kind, identity, url = futures[future]
            try:
                content = future.result()
            except SourceValidationError:
                raise
            except Exception as exc:
                logger.warning("AWS pricing transport failure for %s: %s", url, exc)
                failures.append(url)
                continue
            verified_at = datetime.now(UTC).isoformat()
            if kind in ("claude_page", "claude_map"):
                claude_documents[kind] = content
                continue
            try:
                rows = (
                    parse_kimi_card(content, templates, verified_at=verified_at)
                    if kind == "kimi_card"
                    else parse_model_card(content, identity, templates, source_url=url, verified_at=verified_at)
                    if kind == "card"
                    else parse_catalog(content, identity, source_url=url, verified_at=verified_at)
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise SourceValidationError(f"invalid AWS publication {url}: {exc}") from exc
            fresh.extend(rows)
            if time.monotonic() >= deadline:
                break
        failures.extend(url for future, (_, _, url) in futures.items() if not future.done())
    except TimeoutError:
        failures.extend(url for future, (_, _, url) in futures.items() if not future.done())
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if set(claude_documents) == {"claude_page", "claude_map"}:
        fresh.extend(
            parse_claude_pricing(
                claude_documents["claude_page"],
                claude_documents["claude_map"],
                templates,
                models if models is not None else load_snapshot().models,
                verified_at=datetime.now(UTC).isoformat(),
            )
        )
    return tuple(fresh), tuple(sorted(set(failures)))


def handler(event, context):
    # Neither manual invocations nor scheduled payloads can shrink trusted coverage.
    forbidden = {"required_variants", "manifest", "retirements", "retirement_list", "bundled_required", "sources", "source_urls", "rates"}
    if not isinstance(event, dict) or forbidden.intersection(event):
        raise ValueError("invocation payload cannot override trusted pricing coverage or sources")
    if "report_partial" in event and type(event["report_partial"]) is not bool:
        raise ValueError("report_partial must be a boolean")
    emit_metrics({"PricingRefreshAttempt": 1})
    try:
        with get_db_connection() as conn:
            state = read_active(conn)
        snapshot = load_snapshot()
        # Existing keys are retained even if no longer in the bundled manifest.
        templates = {row.variant_key: row for row in snapshot.rates}
        templates.update({row.variant_key: row for row in state.rows})
        deadline = time.monotonic() + FETCH_SECONDS
        if context is not None and hasattr(context, "get_remaining_time_in_millis"):
            deadline = min(deadline, time.monotonic() + context.get_remaining_time_in_millis() / 1000 - 50)
        fresh, failures = fetch_rates(tuple(templates.values()), deadline, snapshot.models)
        for attempt in range(3):
            try:
                with get_db_connection() as conn:
                    generation, revision, candidate = publish(conn, state.revision, fresh, snapshot.required_variants)
                break
            except PointerConflictError:
                if attempt == 2:
                    raise
                with get_db_connection() as conn:
                    state = read_active(conn)
        metrics = {
            "PricingRequiredVariantsMissing": 0,
            "PricingVariantsRetained": len(candidate.retained_keys),
            "PricingVariantsFresh": len(candidate.fresh_keys),
        }
        partial = bool(candidate.retained_keys or failures)
        metrics["PricingRefreshPartial" if partial else "PricingRefreshSuccess"] = 1
        emit_metrics(metrics, rows=candidate.rows)
        logger.info(
            "Published pricing generation=%s revision=%s variants=%s retained=%s failed_sources=%s",
            generation,
            revision,
            len(candidate.rows),
            len(candidate.retained_keys),
            failures,
        )
        if partial and not event.get("report_partial", False):
            raise PartialRefreshError(
                f"generation {generation} committed with {len(candidate.retained_keys)} retained variants; failed sources={failures}"
            )
        return {
            "status": "published",
            "generation_id": generation,
            "pointer_revision": revision,
            "variants": len(candidate.rows),
            "content_sha256": candidate.content_sha256,
            "partial": partial,
            "retained_variants": len(candidate.retained_keys),
            "fresh_variants": len(candidate.fresh_keys),
            "failed_sources": list(failures),
            "retained_models": sorted({key[0] for key in candidate.retained_keys}),
        }
    except RefreshDeferredError as exc:
        emit_metrics({"PricingRefreshDeferred": 1})
        return {"status": "deferred", "reason": str(exc)}
    except PartialRefreshError:
        raise
    except Exception:
        logger.exception("Pricing refresh rejected; last committed generation retained")
        emit_metrics({"PricingRefreshRejected": 1})
        raise
