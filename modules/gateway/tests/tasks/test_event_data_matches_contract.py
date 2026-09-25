"""Assert the mirrored event vocabularies still equal the frozen schemas.

``src/tasks/events.py`` restates the closed enums, the required-key branches and the
permitted-key set that ``docs/task-api/contracts/v1/schemas/events.schema.json``
owns. The reason is the same one ``limits.py`` has: the gateway container is built
from ``modules/gateway/`` and does not contain ``docs/``, so a runtime read of the
contract would be a code path that passes in a test run and raises in the pod.

Mirroring buys deployability and costs the possibility of drift. This module is the
price: every mirrored set is compared against the schema it came from, in both
directions. A one-way check would let the contract grow a value the validator then
silently refuses — an event kind that becomes unreportable, discovered by the first
producer to try it.

Skipped rather than failed when the contract tree is absent, since a missing file is
not evidence of drift.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tasks import events

SCHEMA_DIR = Path(__file__).resolve().parents[4] / "docs" / "task-api" / "contracts" / "v1" / "schemas"

pytestmark = pytest.mark.skipif(not (SCHEMA_DIR / "events.schema.json").exists(), reason="Task API contract source tree is not present")


@pytest.fixture(scope="module")
def schemas() -> dict[str, dict]:
    return {path.name: json.loads(path.read_text()) for path in SCHEMA_DIR.glob("*.schema.json")}


def resolve(schemas: dict[str, dict], node: dict, document: str) -> tuple[dict, str]:
    """Follow ``$ref`` chains until a concrete subschema is reached.

    The enums these tests compare against mostly live behind a ``$ref`` into
    ``common.schema.json``, so resolving is not a convenience — without it the
    comparison would silently pass against a dict containing only ``$ref``.
    """
    seen = 0
    while "$ref" in node:
        seen += 1
        assert seen < 10, "ref chain does not terminate"
        ref = node["$ref"]
        target_doc, _, pointer = ref.partition("#")
        document = target_doc or document
        node = schemas[document]
        for part in pointer.lstrip("/").split("/"):
            if part:
                node = node[part]
    return node, document


@pytest.fixture(scope="module")
def event_data(schemas) -> dict[str, dict]:
    """Each ``event_data`` property, with its ``$ref`` resolved."""
    properties = schemas["events.schema.json"]["$defs"]["event_data"]["properties"]
    return {name: resolve(schemas, subschema, "events.schema.json")[0] for name, subschema in properties.items()}


def test_permitted_keys_match_the_contract(event_data) -> None:
    """Compared as a set equality, because both directions are failures.

    A key in the contract but not here is a field the surface refuses despite the
    contract permitting it. A key here but not in the contract is one the surface
    would commit into a durable event that then fails ``additionalProperties: false``
    for every reader.
    """
    assert set(events.EVENT_DATA_KEYS) == set(event_data)


def test_every_closed_enum_in_the_contract_is_mirrored(event_data) -> None:
    """No enum may be left unenforced.

    This is the assertion that would have caught the real gap: ``error_code`` is an
    enum in ``results.schema.json`` reached through a ``$ref``, and a hand-written
    mirror that listed the obvious ones would have let any string through as a
    failure code. Derived from the schema rather than from a list maintained here, so
    an enum added to the contract shows up as a failure rather than as silence.
    """
    enum_keys = {name for name, subschema in event_data.items() if "enum" in subschema}

    assert set(events.EVENT_DATA_ENUMS) == enum_keys


@pytest.mark.parametrize("key", sorted(events.EVENT_DATA_ENUMS))
def test_mirrored_enum_values_match_the_contract(event_data, key: str) -> None:
    assert events.EVENT_DATA_ENUMS[key] == frozenset(event_data[key]["enum"]), f"{key} has drifted from the contract enum"


@pytest.mark.parametrize("key", events.EVENT_DATA_POSITIVE_INTEGERS)
def test_mirrored_integer_bounds_match_the_contract(event_data, key: str) -> None:
    assert event_data[key]["type"] == "integer"
    assert event_data[key]["minimum"] == 1


@pytest.mark.parametrize("key", events.EVENT_DATA_BOUNDED_STRINGS)
def test_mirrored_string_bounds_match_the_contract(event_data, key: str) -> None:
    assert event_data[key]["type"] == "string"
    assert (event_data[key]["minLength"], event_data[key]["maxLength"]) == (1, 4000)


def test_event_kinds_match_the_contract(schemas) -> None:
    contract_kinds = resolve(schemas, schemas["events.schema.json"]["$defs"]["event"]["properties"]["type"], "events.schema.json")[0]

    assert set(events.EVENT_TYPES) == set(contract_kinds["enum"])
    assert len(events.EVENT_TYPES) == len(set(events.EVENT_TYPES)), "duplicate kind in the mirrored tuple"


def contract_required_data(schemas: dict[str, dict]) -> dict[str, set[str]]:
    """Per-kind required data keys, read out of the schema's conditional branches.

    ``events.schema.json`` expresses these as ``allOf`` entries of the form
    ``if type == K then data.required == [...]``. Reading them mechanically is the
    only way to compare against ``REQUIRED_EVENT_DATA`` without re-typing the table
    that is under test.
    """
    required: dict[str, set[str]] = {}
    for branch in schemas["events.schema.json"]["$defs"]["event"].get("allOf", []):
        condition = branch.get("if", {}).get("properties", {}).get("type", {})
        kinds = condition.get("enum", [condition["const"]] if "const" in condition else [])
        keys = branch.get("then", {}).get("properties", {}).get("data", {}).get("required", [])
        for kind in kinds:
            required.setdefault(kind, set()).update(keys)
    return required


def test_required_data_keys_match_the_contract_branches(schemas) -> None:
    """The per-kind required-key table is the contract's, not an approximation.

    A kind requiring fewer keys than the contract does accepts an event that fails
    validation downstream; requiring more makes a legal event unreportable. Both are
    silent until a producer hits them, which is why this is asserted per kind with
    the kind named in the failure.
    """
    contract = contract_required_data(schemas)
    assert contract, "no conditional data branches found — the schema structure changed"

    for kind, keys in contract.items():
        assert set(events.REQUIRED_EVENT_DATA.get(kind, ())) == keys, f"{kind} required data has drifted from the contract"

    unmirrored = set(events.REQUIRED_EVENT_DATA) - set(contract)
    assert not unmirrored, f"kinds requiring keys the contract does not: {sorted(unmirrored)}"
