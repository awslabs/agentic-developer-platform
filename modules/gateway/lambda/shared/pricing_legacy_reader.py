"""Preserve legacy non-OpenAI base rates without poisoning ledger transactions.

OpenAI rows in model_pricing are never returned. A successful empty read is
cached too; unavailable/invalid reads retain the last good per-model mapping.
Curated explicit cache rates are merged by the shared resolver at settlement.
"""

import logging
import time

from pricing_policy import is_openai_model, normalize_billing_model_id
from pricing_policy.policy import parse_rate
from pricing_policy.storage import MISSING_SCHEMA_SQLSTATES, RATE_CACHE_TTL_SECONDS, RETRY_MAX_SECONDS, RETRY_MIN_SECONDS, SCHEMA_REPROBE_SECONDS

logger = logging.getLogger(__name__)
_rates = {}
_next_probe = 0.0
_retry_seconds = RETRY_MIN_SECONDS

# Static SQL for savepoint management — these must never interpolate external
# input.  Using literal strings (not f-strings) so static-analysis tools can
# confirm no injection vector exists.
_SQL_SAVEPOINT = "SAVEPOINT pricing_legacy_probe"
_SQL_ROLLBACK = "ROLLBACK TO SAVEPOINT pricing_legacy_probe"
_SQL_RELEASE = "RELEASE SAVEPOINT pricing_legacy_probe"


def _load(conn):
    with conn.cursor() as cur:
        cur.execute(_SQL_SAVEPOINT)
        try:
            cur.execute("SELECT current_setting('statement_timeout')")
            previous_timeout = cur.fetchone()[0]
            cur.execute("SELECT set_config('statement_timeout', '5000ms', true)")
            cur.execute("SELECT model_id, input_price_per_1k_tokens, output_price_per_1k_tokens FROM model_pricing")
            rates = {}
            for model_id, input_price, output_price in cur.fetchall():
                if is_openai_model(model_id):
                    continue
                normalized = normalize_billing_model_id(model_id)
                values = {
                    "input": parse_rate(input_price, field_name="legacy input"),
                    "output": parse_rate(output_price, field_name="legacy output"),
                }
                if normalized in rates and rates[normalized] != values:
                    raise ValueError(f"Conflicting legacy pricing aliases for {normalized}")
                rates[normalized] = values
                rates[model_id] = values
            cur.execute("SELECT set_config('statement_timeout', %s, true)", (previous_timeout,))
        except Exception:
            cur.execute(_SQL_ROLLBACK)
            cur.execute(_SQL_RELEASE)
            raise
        cur.execute(_SQL_RELEASE)
    return rates


def get_legacy_rates(conn, *, force=False):
    """Read non-OpenAI overrides, at most once per cache/retry interval."""
    global _rates, _next_probe, _retry_seconds
    now = time.monotonic()
    if not force and now < _next_probe:
        return _rates
    try:
        candidate = _load(conn)
    except Exception as exc:
        code = getattr(exc, "pgcode", None)
        delay = SCHEMA_REPROBE_SECONDS if code in MISSING_SCHEMA_SQLSTATES else _retry_seconds
        _retry_seconds = min(_retry_seconds * 2, RETRY_MAX_SECONDS)
        _next_probe = time.monotonic() + delay
        logger.warning("Legacy non-OpenAI pricing read failed (%s); retaining prior overrides: %s", code, exc)
    else:
        _rates = candidate
        _retry_seconds = RETRY_MIN_SECONDS
        _next_probe = time.monotonic() + RATE_CACHE_TTL_SECONDS
    return _rates


def reset_for_tests():
    global _rates, _next_probe, _retry_seconds
    _rates = {}
    _next_probe = 0.0
    _retry_seconds = RETRY_MIN_SECONDS
