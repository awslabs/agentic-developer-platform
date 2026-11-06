"""Public contract is a safe development input, not observed provider proof."""

import json

import pytest

from tests.e2e.tools.atlassian_contract import load_contract, require_observed_schema
from tests.e2e.tools.atlassian_oauth_probe import ProbeBlocked


def test_public_methods_have_distinct_schema_slots_scopes_site_and_owners():
    contract = load_contract()
    operations = contract["operations"]
    assert len(operations) == 8
    assert len({entry["schema_name"] for entry in operations}) == 8
    assert {entry["mode"] for entry in operations} == {"read", "write"}
    assert sum(entry["mode"] == "read" for entry in operations) == 4
    assert all(entry["site"].startswith("selected_") for entry in operations)
    assert all(entry["producer"] and entry["consumer"] for entry in operations)
    assert contract["retry"]["write_retry_after_dispatch"] == 0
    assert contract["errors"]["write_uncertain"] == "outcome_unknown"
    for operation in operations:
        with pytest.raises(ProbeBlocked, match="lacks an authenticated"):
            require_observed_schema(operation, None)


@pytest.mark.parametrize("mutate", [
    lambda contract: contract["operations"].pop(),
    lambda contract: contract["operations"][0].update(scope="read:jira-work"),
    lambda contract: contract["operations"][0].update(schema_name=""),
    lambda contract: contract["operations"][0].update(site="any_site"),
    lambda contract: contract["retry"].update(write_retry_after_dispatch=1),
    lambda contract: contract["retry"].update(read_retry_count=99),
    lambda contract: contract["retry"].update(catalogue_max_pages=100),
    lambda contract: contract.update(resource="https://api.atlassian.com/legacy"),
])
def test_reject_incomplete_legacy_or_unsafe_contract(tmp_path, mutate):
    contract = load_contract()
    mutate(contract)
    path = tmp_path / "modified.json"
    path.write_text(json.dumps(contract))
    with pytest.raises(ProbeBlocked):
        load_contract(path)
