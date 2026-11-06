"""PostgreSQL parity for legacy display predicates; isolated schemas only."""

# ruff: noqa: F811 -- imported pytest fixture dependencies
import json
import random
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401

CONTRACT = "dispatch-admission-refusal/v1"
SQL = Path(__file__).parents[2] / "src/orchestration/decision_payload_qualification.sql"


def legacy_flags(reason):
    try:
        value = json.loads(reason or "{}")
    except (ValueError, TypeError):
        return 0
    if not isinstance(value, dict):
        return 0
    return (
        1
        | (2 if all(key in value for key in ("reason", "criterion_ids", "plan_version")) else 0)
        | (4 if isinstance(value.get("attempt"), int) else 0)
        | (8 if value.get("contract") == CONTRACT else 0)
    )


@pytest.fixture
def parser_connection(request):
    try:
        pg_url = request.getfixturevalue("pg_url")
    except pytest.skip.Exception as exc:
        pytest.fail(f"PostgreSQL 16 is required for decision parser parity: {exc}")
    schema = "decision_parity_" + uuid4().hex
    connection = psycopg2.connect(pg_url)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(f'SET search_path="{schema}",pg_catalog')
            cursor.execute("SET statement_timeout = '10s'")
            cursor.execute(SQL.read_text())
            cursor.execute(SQL.with_name("decision_repository_projection.sql").read_text())
        yield connection
    finally:
        connection.rollback()
        with connection, connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        connection.close()


@pytest.fixture
def sql_flags(parser_connection):
    def evaluate(reason):
        with parser_connection, parser_connection.cursor() as cursor:
            cursor.execute("SELECT adp_decision_payload_flags_v1(%s)", (reason,))
            return cursor.fetchone()[0]

    return evaluate


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, 1),
        ("", 1),
        (" ", 0),
        ("{}", 1),
        ("null", 0),
        ("[]", 0),
        ('{"attempt":true}', 5),
        ('{"attempt":false}', 5),
        ('{"attempt":1.0}', 1),
        ('{"attempt":1e400}', 1),
        ('{"attempt":NaN}', 1),
        ('{"attempt":Infinity}', 1),
        ('{"attempt":-Infinity}', 1),
        ('{"attempt":01}', 0),
        ('{"attempt":-NaN}', 0),
        ('{"attempt":InfinityInfinity}', 0),
        ('{"attempt":1,"attempt":null}', 1),
        ('{"attempt":null,"attempt":2}', 5),
        ('{"reason":null,"criterion_ids":null,"plan_version":null}', 3),
        ('{"reason":null,"criterion_ids":null}', 1),
        ('{"contract":"dispatch-admission-refusal/v1"}', 9),
        ('{"contract":"dispatch-admission-refusal/v1","contract":null}', 1),
        ('{"contract":null,"contract":"dispatch-admission-refusal/v1"}', 9),
        (r'{"x":"\u0000","attempt":2}', 5),
        (r'{"\ud800":true,"attempt":2}', 5),
        (r'{"\u0061ttempt":2}', 5),
        (r'{"contract":"\u0064ispatch-admission-refusal/v1"}', 9),
        (r'{"contract":"\u0000dispatch-admission-refusal/v1"}', 1),
        (r'{"attempt":"NaN"}', 1),
        (r'{"attempt":"\\ud800"}', 1),
        (r'{"attempt":"\x00"}', 0),
    ],
)
def test_postgres_decision_predicate_independent_cases(sql_flags, reason, expected):
    assert legacy_flags(reason) == expected
    assert sql_flags(reason) == expected


@pytest.mark.parametrize(
    "actor, actor_kind, payload, expected",
    [
        ("system:repository-evaluation", "service", '{"action":"repository_evaluation_blocked"}', 9),
        ("system:repository-evaluation", "service", '{"contract":"repository-evaluation-refusal/v1","scope":{}}', 9),
        ("system:repository-evaluation", "service", "[]", None),
        ("system:repository-evaluation", "service", "malformed", None),
        ("system:repository-evaluation", "human", "{}", None),
        ("other", "service", "{}", None),
    ],
)
def test_repository_refusal_category_matches_sqlite_and_preserves_v1(parser_connection, actor, actor_kind, payload, expected):
    from src.orchestration.decision_sqlite import category

    assert category("transition_rejected", actor, actor_kind, None, payload) == expected
    with parser_connection, parser_connection.cursor() as cursor:
        cursor.execute(
            "SELECT adp_decision_category_v1('transition_rejected', %s, %s, NULL, %s), "
            "adp_decision_category_v2('transition_rejected', %s, %s, NULL, %s)",
            (actor, actor_kind, payload, actor, actor_kind, payload),
        )
        assert cursor.fetchone() == (None, expected)


@pytest.mark.parametrize(
    "actor, actor_kind, expected",
    [
        ("system:repository-evaluation", "service", 10),
        ("system:repository-evaluation", "human", 3),
        ("other", "service", 3),
    ],
)
def test_repository_result_has_its_own_bounded_category(parser_connection, actor, actor_kind, expected):
    from src.orchestration.decision_sqlite import category

    payload = '{"action":"repository_evaluation"}'
    assert category("result_observed", actor, actor_kind, payload, None) == expected
    with parser_connection, parser_connection.cursor() as cursor:
        cursor.execute(
            "SELECT adp_decision_category_v1('result_observed', %s, %s, %s, NULL), adp_decision_category_v2('result_observed', %s, %s, %s, NULL)",
            (actor, actor_kind, payload, actor, actor_kind, payload),
        )
        assert cursor.fetchone() == (3, expected)


def test_postgres_decision_predicate_python_boundaries(sql_flags):
    assert sys.get_int_max_str_digits() == 4300
    for length in (4299, 4300, 4301, 10000):
        for suffix in ("", ".0", "e40000000000000000000"):
            reason = '{"attempt":' + "9" * length + suffix + "}"
            assert sql_flags(reason) == legacy_flags(reason)
    for escape in (
        r"\u0000",
        r"\ud800",
        r"\udc00",
        r"\ud800\udc00",
        r"\udfff",
        r"\ud8A0",
        r"\udD00",
        r"\u0061ttempt",
        r"\\ud800",
        r"\n",
        r"\x00",
        "NaN",
        "Infinity",
        r"\uFFFF",
    ):
        for reason in ('{"x":"' + escape + '","attempt":2}', '{"' + escape + '":1,"attempt":2}', '{"attempt":"' + escape + '"}'):
            assert sql_flags(reason) == legacy_flags(reason)


def test_postgres_decision_predicate_generated_parity(sql_flags):
    rng = random.Random(7775)
    values = [
        None,
        True,
        False,
        0,
        1,
        -1,
        1.0,
        1e308,
        float("nan"),
        float("inf"),
        float("-inf"),
        "",
        "NaN",
        "Infinity",
        "\\ud800",
        "\0",
        "\ud800",
        "\udc00",
        CONTRACT,
        [],
        {},
    ]
    cases = []
    for _ in range(3000):
        value = {
            rng.choice(["attempt", "contract", "reason", "criterion_ids", "plan_version", "x", "\0", "\ud800"]): rng.choice(values) for _ in range(8)
        }
        cases.append(json.dumps(value, ensure_ascii=True))
    for reason in cases[-200:]:
        cases.extend((reason[:-1], reason + "x", reason.replace(":", ":,", 1)))
    for reason in cases:
        assert sql_flags(reason) == legacy_flags(reason), reason


def test_postgres_parser_large_payload_resources(sql_flags, record_property):
    measurements = {}
    for size in (8192, 16384, 32768):
        reason = json.dumps({"attempt": 1, "values": list(range(size))})
        started = time.monotonic()
        assert sql_flags(reason) == legacy_flags(reason) == 5
        measurements[f"array_{size}"] = time.monotonic() - started
    for reason in (
        json.dumps({"attempt": 2, "large": "x" * 262144}),
        '{"attempt":2,"nested":' + "[" * 512 + "0" + "]" * 512 + "}",
        '{"attempt":2,"nested":' + "[" * 512 + "0" + "]" * 511 + "}",
    ):
        assert sql_flags(reason) == legacy_flags(reason)
    record_property("parser_seconds", json.dumps(measurements, sort_keys=True))
    print("parser_seconds", json.dumps(measurements, sort_keys=True))


def test_postgres_parser_deep_payload_and_timeout_outcomes(sql_flags, parser_connection):
    reason = '{"nested":' + "[" * 10000 + "0" + "]" * 10000 + "}"
    with pytest.raises(RecursionError):
        legacy_flags(reason)
    assert sql_flags(reason) == 1
    with parser_connection, parser_connection.cursor() as cursor:
        cursor.execute("SET statement_timeout = '1ms'")
    try:
        with pytest.raises(psycopg2.errors.QueryCanceled):
            sql_flags(json.dumps({"values": list(range(32768))}))
    finally:
        with parser_connection, parser_connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '10s'")


def test_postgres_projection_qualifies_before_ranking_and_keeps_clearers(parser_connection):
    from psycopg2.extras import execute_values

    from src.orchestration.decision_projection import projection_sql

    rows = []

    def add(identity, node, kind, reason=None, *, actor="human", actor_kind="human", rejection=None, org="owner"):
        rows.append((identity, org, "flow", node, kind, actor, actor_kind, reason, rejection))

    add("01", "stall", "node_stalled")
    add("02", "stall", "node_halted")
    add("03", "gate", "gate_approved")
    add("04", "gate", "gate_rejected", "verbatim")
    add("05", "dispatch", "node_dispatched", '{"attempt":1,"run_id":"old"}')
    add("06", "dispatch", "node_dispatched", '{"attempt":2,"run_id":"new"}')
    add("07", "dispatch", "node_dispatched", "malformed")
    add("08", "result", "result_observed", '{"attempt":true}')
    add("09", "result", "result_observed", '{"attempt":2}')
    add("10", "result", "result_observed", "{}")
    add("11", "result", "result_observed", "[]")
    add("12", "waiver", "evaluation_waived", '{"reason":"ok","criterion_ids":[],"plan_version":1}')
    add("13", "waiver", "evaluation_waived", '{"reason":"missing keys"}')
    add("14", "waiver", "evaluation_waived", '{"reason":"ok","criterion_ids":[],"plan_version":1}', actor_kind="service")
    refusal = '{"contract":"dispatch-admission-refusal/v1"}'
    add("15", "admission", "transition_rejected", actor="system:orchestration-dispatch", actor_kind="service", rejection=refusal)
    add("16", "admission", "transition_rejected", actor="wrong", actor_kind="service", rejection=refusal)
    add("17", "admission", "node_dispatched", "broken")
    add("18", "wait", "evaluation_evidence_wait", "{}", actor="system:orchestration-tick", actor_kind="service")
    add("19", "wait", "evaluation_evidence_ready", "broken", actor="system:orchestration-tick", actor_kind="service")
    add("20", "wait", "evaluation_evidence_ready", actor="wrong", actor_kind="service")
    add("21", "retry", "developer_retry_checked", "{}", actor="system:developer-recovery", actor_kind="service")
    add("22", "retry", "developer_retry_checked", "broken", actor="system:developer-recovery", actor_kind="service")
    add("23", "retry", "developer_retry_checked", "{}", actor="wrong", actor_kind="service")
    add("99", "dispatch", "node_dispatched", '{"attempt":9}', org="foreign")
    nodes = ["stall", "gate", "dispatch", "result", "waiver", "admission", "wait", "retry"]
    with parser_connection, parser_connection.cursor() as cursor:
        cursor.execute("""CREATE TABLE orchestration_decisions
            (id text PRIMARY KEY, org_id text, flow_id text, node_id text, kind text, actor_id text,
             actor_kind text, reason text, rejection_reason text, created_at timestamptz DEFAULT '2026-01-01Z')""")
        execute_values(
            cursor, "INSERT INTO orchestration_decisions (id,org_id,flow_id,node_id,kind,actor_id,actor_kind,reason,rejection_reason) VALUES %s", rows
        )

        def query(dispatch_only=False):
            return (
                projection_sql(dispatch_only=dispatch_only)
                .replace(":org_id", "%(org_id)s")
                .replace(":flow_id", "%(flow_id)s")
                .replace(":node_ids", "%(node_ids)s")
            )

        parameters = {"org_id": "owner", "flow_id": "flow", "node_ids": json.dumps(nodes)}
        cursor.execute(query(), parameters)
        selected = cursor.fetchall()
        assert {row[0] for row in selected} == {"02", "04", "06", "07", "09", "10", "12", "15", "17", "18", "19", "21"}
        assert len(selected) <= 11 * len(nodes)
        cursor.execute(query(True), parameters)
        assert [row[0] for row in cursor.fetchall()] == ["06"]
        cursor.execute(query(), {**parameters, "flow_id": "missing"})
        assert cursor.fetchall() == []
