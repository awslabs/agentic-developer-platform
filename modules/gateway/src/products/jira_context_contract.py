"""Load the single shared Jira context contract in a checkout or staged image."""

import importlib.util
from pathlib import Path

gateway = Path(__file__).resolve().parents[2]
source = gateway.parent / "tools" / "adp_tools" / "jira_context.py"
path = source if source.is_file() else gateway / "contracts" / "jira-context" / "contract.py"
spec = importlib.util.spec_from_file_location("adp_jira_context_contract", path)
if spec is None or spec.loader is None:
    raise RuntimeError("Jira context contract unavailable")
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)
JiraContextQuery = contract.JiraContextQuery
jira_jql = contract.jira_jql
jira_board_in_project = contract.jira_board_in_project
context_tool = contract.context_tool
CONTEXT_OPERATION = contract.CONTEXT_OPERATION
CONTEXT_TOOL = contract.CONTEXT_TOOL
