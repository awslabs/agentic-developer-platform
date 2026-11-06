"""Local terminal acknowledgements remain valid shared Operation projections."""

import json
import runpy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

CONTRACT = Path(__file__).resolve().parents[4] / "docs/l5-services/contracts/v1"


@pytest.mark.parametrize("state", ["cancelled", "abandoned"])
def test_owner_terminal_acknowledgement_has_an_additive_strict_contract(state):
    schema = json.loads((CONTRACT / "schemas.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/Operation"})
    projection = {
        "operation_id": "original-operation",
        "action": "sync_knowledge",
        "service_id": "devops",
        "state": state,
        "revision": 2,
        "gaps": ["knowledge_publication_" + state],
        "binding_id": "original-binding",
    }
    validator.validate(projection)
    assert not validator.is_valid({**projection, "state": "invented_terminal"})
    assert not validator.is_valid({**projection, "native_cleanup_performed": True})
    assert not validator.is_valid({**projection, "revision": 0})
    assert not validator.is_valid({key: value for key, value in projection.items() if key != "operation_id"})
    assert schema["$defs"]["Operation"]["properties"]["readback_ref"] == {"$ref": "#/$defs/RecordRef"}


def test_generated_shared_artifacts_keep_terminal_states():
    generated = runpy.run_path(str(CONTRACT / "catalog.py"))["artifacts"]()
    for filename, expected in generated.items():
        assert (CONTRACT / filename).read_text() == expected
    tools = json.loads(generated["mcp-tools.json"])
    operation_tools = [tool for tool in tools if "Operation" in tool["outputSchema"].get("$defs", {})]
    assert operation_tools
    for tool in operation_tools:
        assert {"cancelled", "abandoned"} <= set(tool["outputSchema"]["$defs"]["Operation"]["properties"]["state"]["enum"])
