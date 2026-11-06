"""One bounded client setup journey; no implicit retries of paid effects."""

from clients import verify_setup
from urllib.parse import quote


def clients_and_setup(case, limits, driver):
    selection = dict(case["selected"])
    if (case["run_input"]["binding_id"] != selection["binding_id"]
            or case["run_input"]["sources"] != selection["sources"]):
        raise AssertionError("MCP input differs from selected binding or source revisions")
    before = driver.cli_configuration()
    if (before["configuration_id"] != selection["configuration_id"]
            or before["scope"] != selection["scope"]
            or before["settings"]["region"] != case["initial_region"]):
        raise AssertionError("CLI setup starts from an unexpected configuration or Product")
    path = (f"/v1/products/{quote(case['scope']['product_id'], safe='')}/projects/"
            f"{quote(case['scope']['project_id'], safe='')}/services/security")
    independent = driver.read_api(path + "/configuration")
    if (independent["configuration_id"] != before["configuration_id"]
            or independent["revision"] != before["revision"]
            or independent["hash"] != before["hash"]
            or before["etag"] != f'"{independent["hash"]}"'):
        raise AssertionError("CLI profile does not match independently selected deployment revision")
    binding = driver.cli_binding(selection["binding_id"])
    independent_binding = driver.read_api(path + "/bindings/" + quote(selection["binding_id"], safe=""))
    if binding != independent_binding or binding["connection_ref"] != selection["connection_ref"]:
        raise AssertionError("CLI binding does not match independent provider selection")
    if (binding["configuration_id"] != before["configuration_id"] or binding["state"] != "ready"):
        raise AssertionError("selected provider binding is not ready for setup")
    driver.verify_provider_authority(case["provider"])
    updated = driver.cli_update(case["cli_settings_file"], before["etag"], case["cli_receipt"])
    if (updated["configuration_id"] != before["configuration_id"]
            or updated["revision"] != before["revision"] + 1
            or updated["settings"]["region"] != case["cli_region"]):
        raise AssertionError("CLI edit did not preserve config identity and selected region")
    edited = driver.ui_edit(case["ui_region"], case["ui_reason"])
    if (edited["configuration_id"] != updated["configuration_id"]
            or edited["revision"] != updated["revision"] + 1
            or edited["settings"]["region"] != case["ui_region"]):
        raise AssertionError("served browser did not save the conditional UI edit")
    cli_readback = driver.cli_configuration()
    if cli_readback["revision"] != edited["revision"]:
        raise AssertionError("CLI did not read the served UI revision")
    run_input = {**case["run_input"], "configuration_revision": edited["revision"]}
    if run_input["binding_id"] != selection["binding_id"] or run_input["sources"] != selection["sources"]:
        raise AssertionError("MCP input differs from selected binding or source revisions")
    submitted = driver.mcp_submit(run_input, case["mcp_receipt"])
    selection["run_id"] = submitted["run_id"]
    selection["max_attempts"] = limits["max_attempts"]
    selection["effects"] = {"cli_update": before["configuration_id"],
                            "ui_edit": edited["configuration_id"], "mcp_submit": submitted["run_id"]}
    observations = {"cli": driver.cli_observations(selection["binding_id"], submitted["run_id"]),
                    "ui": driver.ui_observations(selection["binding_id"], submitted["run_id"]),
                    "mcp": driver.mcp_observations(selection["binding_id"], submitted["run_id"])}
    attempts = driver.attempts(selection)
    result = verify_setup(selection, observations, attempts, driver.read_api)
    if result["configuration_revision"] != edited["revision"]:
        raise AssertionError("independent readback lost the UI edit")
    return result
