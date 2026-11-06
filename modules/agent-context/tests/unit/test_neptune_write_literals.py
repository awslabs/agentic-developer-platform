"""Repository text stays data in every Neptune HTTP write and delete traversal."""

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

INGESTION = Path(__file__).resolve().parents[2] / "images" / "ingestion"
PAYLOADS = [
    "x').V().drop();//",
    'x").V().drop();//',
    "trailing\\",
    "\\');g.V().drop();//",
    "literal\\u0027",
    "line\ncarriage\rtab\tbackspace\bformfeed\fnull\x00",
    "O'Reilly/日本語/🧪",
    '${g.V().drop()} and "quotes"',
]


@pytest.fixture
def ingester(monkeypatch):
    monkeypatch.syspath_prepend(str(INGESTION))
    spec = importlib.util.spec_from_file_location("neptune_literal_ingester", INGESTION / "ingest-repo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "NEPTUNE_ENDPOINT", "neptune.example.test")
    monkeypatch.setattr(module, "_get_neptune_signed_headers", Mock(return_value={"Authorization": "test-signature"}))
    monkeypatch.setattr(module.requests, "post", Mock(return_value=Mock(status_code=200)))
    return module


def literals(query):
    """Decode complete double-quoted data tokens, leaving traversal syntax visible."""
    values = []

    def decode(match):
        values.append(json.loads(match.group()))
        return "DATA"

    syntax = re.sub(r'"(?:[^"\\]|\\.)*"', decode, query)
    return syntax, values


@pytest.mark.parametrize("payload", PAYLOADS)
def test_vertex_keys_values_and_ids_cannot_add_traversal_steps(ingester, payload):
    assert ingester._write_to_neptune([{"entity_id": payload, payload: payload, "count": 5}], [], "org/repo")
    request = ingester.requests.post.call_args.kwargs
    syntax, values = literals(json.loads(request["data"])["gremlin"])
    assert syntax == (
        "g.V().has('entity_id', DATA).fold()"
        ".coalesce(unfold(), addV('entity').property('entity_id', DATA))"
        ".property(DATA, DATA).property(DATA, DATA)"
    )
    assert values == [payload, payload, payload, payload, "count", "5"]
    assert request["headers"] == {"Authorization": "test-signature"}
    assert ingester._get_neptune_signed_headers.call_args.args[2] == request["data"]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_edge_endpoints_and_labels_cannot_add_traversal_steps(ingester, payload):
    assert ingester._write_to_neptune([], [{"from": payload, "to": payload, "type": payload}], "org/repo")
    syntax, values = literals(json.loads(ingester.requests.post.call_args.kwargs["data"])["gremlin"])
    assert syntax == (
        "g.V().has('entity_id', DATA).as('a').V().has('entity_id', DATA).as('b')"
        ".select('a').coalesce(  outE(DATA).where(inV().as('b')),  addE(DATA).to('b'))"
    )
    assert values == [payload] * 4


@pytest.mark.parametrize("payload", PAYLOADS)
def test_clear_retains_the_exact_repo_and_source_fence(ingester, payload):
    assert ingester._clear_repo_structural_graph(payload)
    syntax, values = literals(json.loads(ingester.requests.post.call_args.kwargs["data"])["gremlin"])
    assert syntax == "g.V().has('repo', DATA).has('source', 'code-index').drop()"
    assert values == [payload]
