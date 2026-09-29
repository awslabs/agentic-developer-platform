"""Injection and authorization regressions for the personal-context graph.

Set PERSONAL_GRAPH_TEST_BOLT_URL to a disposable local Neo4j instance to execute
actual Cypher. This validates query semantics, not live Neptune compatibility.
"""

import ast
import inspect
import json
import os
from unittest.mock import Mock
from urllib.parse import parse_qs

import pytest

from personal_context import graph
from personal_context.identity import CallerIdentity, IdentityError, extract_identity

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
IDENTITY = CallerIdentity(OWNER, "org-acme")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(graph, "PERSONAL_CONTEXT_GRAPH_ENABLED", True)
    monkeypatch.setattr(graph, "NEPTUNE_ENDPOINT", "example.neptune.amazonaws.com")


@pytest.mark.parametrize(
    "tenant",
    [
        "",
        " ",
        " org-acme ",
        "org/other",
        "org'",
        "org\\",
        "org\n",
        "org\x00",
        "../x",
        "équipe",
        "a" * 129,
    ],
)
def test_invalid_tenant_rejected_at_both_identity_boundaries(tenant):
    with pytest.raises(IdentityError):
        CallerIdentity(OWNER, tenant)
    with pytest.raises(IdentityError):
        extract_identity({"X-Owner-Sub": OWNER, "X-Tenant-Id": tenant})


@pytest.mark.parametrize("tenant", ["org-acme", "org_123", OWNER, "01ABC123", "a" * 128])
def test_supported_tenant_ids(tenant):
    assert extract_identity({"X-Owner-Sub": OWNER, "X-Tenant-Id": tenant}).tenant_id == tenant


@pytest.mark.parametrize("value", ["x'\\", "x\\');g.V().drop();//", "x\n\x00", "雪"])
def test_values_never_change_query_text(enabled, monkeypatch, value):
    execute = Mock(return_value={"results": [{"entry_id": value}]})
    monkeypatch.setattr(graph, "_execute_cypher", execute)
    graph.upsert_vertex(value, OWNER, IDENTITY.tenant_id, value, value, "private")
    assert execute.call_args.args == (
        graph._UPSERT,
        {
            "entry_id": value,
            "owner_sub": OWNER,
            "tenant_id": IDENTITY.tenant_id,
            "entry_type": value,
            "persona": value,
            "visibility": "private",
        },
    )
    graph.add_edge(value, value, "supports", {value: value}, identity=IDENTITY)
    assert execute.call_args.args[0] == graph._EDGE_QUERIES["supports"]
    assert execute.call_args.args[1]["properties"] == {value: value}
    graph.get_neighbors(value, IDENTITY)
    assert execute.call_args.args[0] == graph._NEIGHBORS
    assert execute.call_args.args[1]["entry_id"] == value
    graph.remove_vertex(value, identity=IDENTITY)
    assert execute.call_args.args[0] == graph._REMOVE
    assert execute.call_args.args[1]["entry_id"] == value


def test_mutations_without_identity_never_send_requests(enabled, monkeypatch):
    execute = Mock()
    monkeypatch.setattr(graph, "_execute_cypher", execute)
    assert graph.add_edge("a", "b", "supports") is False
    assert graph.remove_vertex("a") is False
    assert graph.upsert_vertex("a", OWNER, "org'", "learning", "developer", "private") is False
    assert graph.add_edge("a", "b", "supports`]->(v) DELETE v //", identity=IDENTITY) is False
    execute.assert_not_called()


def test_neptune_http_parameter_envelope(monkeypatch):
    import httpx

    value = "quote'\\ &query=DELETE n\n"
    post = Mock(return_value=httpx.Response(200, json={"results": []}))
    sign = Mock(return_value={"Content-Type": "application/x-www-form-urlencoded"})
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(graph, "_sign_request", sign)
    monkeypatch.setattr(graph, "NEPTUNE_ENDPOINT", "fixture.neptune.amazonaws.com")
    graph._execute_cypher(
        graph._REMOVE, {"entry_id": value, "owner_sub": OWNER, "tenant_id": "org-acme"}
    )
    args, kwargs = post.call_args
    assert args[0] == "https://fixture.neptune.amazonaws.com:8182/openCypher"
    payload = parse_qs(kwargs["content"])
    assert payload["query"] == [graph._REMOVE]
    assert json.loads(payload["parameters"][0])["entry_id"] == value
    assert sign.call_args.args == ("POST", args[0], kwargs["content"])
    assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_query_construction_cannot_interpolate_caller_text():
    tree = ast.parse(inspect.getsource(graph))
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name != "_get_neptune_url":
            assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(fn))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_execute_cypher"
        ):
            query = node.args[0]
            assert (
                isinstance(query, ast.Name) and query.id in {"_UPSERT", "_NEIGHBORS", "_REMOVE"}
            ) or (
                isinstance(query, ast.Subscript)
                and isinstance(query.value, ast.Name)
                and query.value.id == "_EDGE_QUERIES"
            )
    # Edge labels are source constants; neither caller data nor environment may
    # supply identifiers to the only query-template substitution.
    assert graph.VALID_EDGE_TYPES == {
        "derived_from",
        "contradicts",
        "supports",
        "exemplifies",
        "cross_persona",
    }


@pytest.fixture
def real_graph(enabled, monkeypatch):
    uri = os.environ.get("PERSONAL_GRAPH_TEST_BOLT_URL")
    if not uri:
        pytest.skip("disposable local Cypher server not configured")
    from urllib.parse import urlsplit

    from neo4j import GraphDatabase

    if urlsplit(uri).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Tests require a disposable loopback database")
    driver = GraphDatabase.driver(uri, auth=None)
    driver.verify_connectivity()
    # Each test runs in a transaction that is always rolled back.
    with driver.session() as session:
        tx = session.begin_transaction()

        def execute(query, parameters):
            return {"results": tx.run(query, parameters).data()}

        monkeypatch.setattr(graph, "_execute_cypher", execute)
        yield tx
        tx.rollback()
    driver.close()


def test_real_query_roundtrip_and_injection(real_graph):
    hostile = "entry'\\');g.V().drop();//"
    assert graph.upsert_vertex(hostile, OWNER, "org-acme", "learning", "developer", "private")
    assert graph.upsert_vertex("neighbor", OWNER, "org-acme", "learning", "developer", "private")
    for kind in graph.VALID_EDGE_TYPES:
        assert graph.add_edge(hostile, "neighbor", kind, {"key'\\": hostile}, identity=IDENTITY)
    neighbors = graph.get_neighbors(hostile, IDENTITY)
    assert {n["edge_type"] for n in neighbors} == graph.VALID_EDGE_TYPES
    assert all(n["entry_id"] == "neighbor" and n["direction"] == "outgoing" for n in neighbors)
    assert all(n["direction"] == "incoming" for n in graph.get_neighbors("neighbor", IDENTITY))
    properties = real_graph.run("MATCH ()-[e]->() RETURN properties(e) AS p").data()
    assert len(properties) == 5
    assert all(row["p"]["key'\\"] == hostile for row in properties)
    assert graph.remove_vertex(hostile, identity=IDENTITY)
    assert graph.get_neighbors("neighbor", IDENTITY) == []
    assert real_graph.run("MATCH (n) RETURN count(n) AS total").single()["total"] == 1


def test_real_tenant_scope_at_start_neighbor_and_mutations(real_graph):
    for entry, owner, tenant, visibility in [
        ("own", OWNER, "org-acme", "private"),
        ("shared", OTHER, "org-acme", "shared"),
        ("private", OTHER, "org-acme", "private"),
        ("foreign-same-owner", OWNER, "org-other", "shared"),
        ("foreign", OTHER, "org-other", "shared"),
    ]:
        assert graph.upsert_vertex(entry, owner, tenant, "learning", "developer", visibility)
    # Seed even forbidden historical edges to ensure reads enforce their own ACL.
    real_graph.run(
        "MATCH (a {entry_id: 'own'}), (b) WHERE b <> a CREATE (a)-[:supports]->(b)"
    ).consume()
    assert {r["entry_id"] for r in graph.get_neighbors("own", IDENTITY)} == {"shared"}
    for start in ("private", "foreign", "foreign-same-owner"):
        assert graph.get_neighbors(start, IDENTITY) == []
        assert not graph.add_edge("own", start, "contradicts", identity=IDENTITY)
        graph.remove_vertex(start, identity=IDENTITY)
        assert (
            real_graph.run("MATCH (n {entry_id: $id}) RETURN count(n) AS n", id=start).single()["n"]
            == 1
        )
    assert not graph.add_edge("shared", "own", "supports", identity=IDENTITY)
    # Upsert may create a caller-owned entry with the same external id, but must
    # not overwrite another tenant's vertex or its properties.
    assert graph.upsert_vertex("foreign", OWNER, "org-acme", "pattern", "reviewer", "private")
    foreign = real_graph.run(
        "MATCH (n {entry_id: 'foreign', tenant_id: 'org-other'}) RETURN n.owner_sub AS owner, n.type AS type"
    ).single()
    assert dict(foreign) == {"owner": OTHER, "type": "learning"}
