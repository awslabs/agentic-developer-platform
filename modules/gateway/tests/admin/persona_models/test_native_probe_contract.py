"""Native probe contracts are generated once and packaged identically."""

import json
from pathlib import Path

from src.admin.persona_models import native_probe_contract as contract
from src.admin.persona_models.catalogue import persona_harness_contract_revision
from src.tasks.personas import TASK_PERSONAS


def test_native_manifest_packaging_and_sdk_identity():
    modules = Path(__file__).resolve().parents[4]
    gateway = Path(contract.__file__).with_name("native-probe-manifest.json").read_bytes()
    for path in [
        "agent-factory/agent/src/invocability-probe/native-probe-manifest.json",
        "agent-factory/codex-reviewer/scripts/native-probe-manifest.json",
    ]:
        assert (modules / path).read_bytes() == gateway
    package = json.loads((modules / "agent-factory/codex-reviewer/package.json").read_text())
    assert package["dependencies"]["@openai/codex-sdk"] == contract.NATIVE_PROBE_REVISION
    for persona in contract.NATIVE_PROBE_PERSONAS:
        assert persona not in TASK_PERSONAS
        assert persona_harness_contract_revision(persona) == contract.NATIVE_PROBE_REVISION
    for model in ["openai.gpt-6-astra", "openai.gpt-6-sol", "openai.gpt-6-luna"]:
        developer = contract.native_request_shape("agent-codex-developer", model)
        reviewer = contract.native_request_shape("agent-codex-reviewer", model)
        assert developer and reviewer and developer != reviewer
    assert contract.native_request_shape("agent-task-gpt-developer", "openai.gpt-6-sol") is None


def test_report_profiles_are_distinct_and_packaged_with_exact_catalogue():
    modules = Path(__file__).resolve().parents[4]
    gateway = Path(contract.__file__).with_name("report-probe-manifest.json").read_bytes()
    for path in [
        "agent-factory/agent/src/invocability-probe/report-probe-manifest.json",
        "agent-factory/codex-harness/scripts/report-probe-manifest.json",
    ]:
        assert (modules / path).read_bytes() == gateway
    capture_catalogue = (modules / "agent-factory/codex-harness/scripts/report-probe-catalogue.json").read_bytes()
    assert capture_catalogue == (modules / "gateway/src/agentauth/codex-github-catalogue.json").read_bytes()
    for model in ["openai.gpt-6-astra", "openai.gpt-6-sol", "openai.gpt-6-luna"]:
        shapes = [contract.native_request_shape(persona, model) for persona in contract.NATIVE_PROBE_PERSONAS]
        assert all(shapes) and len(set(shapes)) == len(shapes)
