"""Trusted-admin PostgreSQL adapter for immutable V2 pricing publication.

Fetches occur outside this adapter's write transaction. Pointer conflicts force a
rebuild from the winner, and the required manifest is never supplied by an event.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from psycopg2.extras import RealDictCursor, execute_values

from pricing_policy.policy import POLICY_VERSION, SUPPORTED_POLICY_VERSIONS, RateRow
from pricing_policy.refresh import assemble_candidate, canonical_content_hash


class PointerConflictError(RuntimeError):
    pass


class RefreshDeferredError(RuntimeError):
    pass


@dataclass(frozen=True)
class ActiveState:
    generation_id: int
    revision: int
    rows: tuple[RateRow, ...]


def read_active(conn, *, lock=False) -> ActiveState:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '15s'")
        cur.execute("SELECT to_regclass('model_pricing_active') AS relation")
        if cur.fetchone()["relation"] is None:
            raise RefreshDeferredError("schema_absent")
        cur.execute("SELECT * FROM model_pricing_active WHERE singleton" + (" FOR UPDATE" if lock else ""))
        pointer = cur.fetchone()
        if not pointer:
            raise RuntimeError("V2 singleton pointer missing")
        if pointer["refresh_paused"]:
            raise RefreshDeferredError("paused")
        if not pointer["consumers_enabled"] or pointer["current_generation_id"] is None:
            raise RefreshDeferredError("consumers_disabled_or_unseeded")
        cur.execute(
            "SELECT status,schema_version,policy_version,required_variants,content_sha256 FROM model_pricing_generations WHERE generation_id=%s",
            (pointer["current_generation_id"],),
        )
        generation = cur.fetchone()
        if not generation or generation["status"] != "validated":
            raise RuntimeError("active generation is not validated")
        cur.execute("SELECT * FROM model_pricing_rates_v2 WHERE generation_id=%s", (pointer["current_generation_id"],))
        rows = tuple(RateRow.from_mapping(dict(row)) for row in cur.fetchall())
        if not rows:
            raise RuntimeError("active validated generation contains no rates")
        if generation["schema_version"] != 2:
            raise RuntimeError("active generation has incompatible schema version")
        if generation["policy_version"] not in SUPPORTED_POLICY_VERSIONS:
            raise RuntimeError("active generation has incompatible policy version")
        required = {tuple(key) for key in generation["required_variants"]}
        if required != {row.variant_key for row in rows}:
            raise RuntimeError("active generation coverage does not match its manifest")
        if canonical_content_hash(rows) != generation["content_sha256"]:
            raise RuntimeError("active generation content hash mismatch")
        return ActiveState(pointer["current_generation_id"], pointer["pointer_revision"], rows)


def publish(conn, expected_revision: int, fresh: tuple[RateRow, ...], bundled_required):
    """Caller commits; every failure rolls back generation, rates and pointer."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '5s'")
        cur.execute("SET LOCAL statement_timeout = '15s'")
    state = read_active(conn, lock=True)
    if state.revision != expected_revision:
        raise PointerConflictError(f"pointer revision changed from {expected_revision} to {state.revision}")
    candidate = assemble_candidate(state.rows, fresh, bundled_required)
    version = "refresh-" + candidate.content_sha256
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO model_pricing_generations
            (schema_version,policy_version,snapshot_version,status,required_variants,content_sha256,created_at)
            VALUES (2,%s,%s,'building',%s::jsonb,%s,now()) RETURNING generation_id""",
            (POLICY_VERSION, version, json.dumps(sorted(candidate.required)), candidate.content_sha256),
        )
        generation_id = cur.fetchone()[0]
        columns = (
            "model_id",
            "geography",
            "service_tier",
            "context_tier",
            "region",
            "max_input_tokens",
            "input_price_per_1k_tokens",
            "output_price_per_1k_tokens",
            "cache_read_price_per_1k_tokens",
            "cache_write_price_per_1k_tokens",
            "cache_write_1h_price_per_1k_tokens",
            "cache_write_policy",
            "source",
            "source_url",
            "source_content_sha256",
            "source_effective_at",
            "verified_at",
            "snapshot_version",
        )
        values = [(generation_id, *(getattr(row, name) for name in columns)) for row in candidate.rows]
        execute_values(cur, "INSERT INTO model_pricing_rates_v2 (generation_id," + ",".join(columns) + ") VALUES %s", values)
    # Validate what PostgreSQL actually stored before making it immutable/visible.
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation_id,))
        stored = tuple(RateRow.from_mapping(dict(row)) for row in cur.fetchall())
        if {row.variant_key for row in stored} != candidate.required or canonical_content_hash(stored) != candidate.content_sha256:
            raise RuntimeError("stored generation coverage/content hash mismatch")
        cur.execute("UPDATE model_pricing_generations SET status='validated',validated_at=now() WHERE generation_id=%s", (generation_id,))
        cur.execute(
            """UPDATE model_pricing_active SET current_generation_id=%s,
            pointer_revision=pointer_revision+1,updated_at=now() WHERE singleton""",
            (generation_id,),
        )
    return generation_id, state.revision + 1, candidate
