"""The AgentCore shared-memory Terraform stays switch-gated and least-privilege (#7901).

``modules/gateway/infra/agentcore-memory.tf`` provisions what ``agentcore_recall()``
(``src/agentauth/chat_model.py``) expects: a memory, one long-term strategy, the
gateway's data-plane grant and the SSM parameters the renderers read. Three things
would fail silently in a deploy and are pinned here instead:

1. A resource without the enable-gated ``count`` is created in every environment,
   including ones that never asked for memory (AC-01).
2. The gateway's IAM policy drifting from the calls the code actually makes — too
   wide and the gateway can touch memory it does not own; too narrow and recall or
   deletion 403s in production only (AC-02). The oracle is the code: every
   ``client.<method>(`` call site under ``src/agentauth/agentcore_*.py``.
3. The strategy namespace template not matching ``owner_namespace()``: extraction
   succeeds, recall finds nothing, and the ``namespaces == [namespace]`` check
   rejects every record (AC-03).

Plus the namespace key must be a KMS-encrypted SecureString minted by
``random_bytes`` (AC-04) and the dev switch must stay commented (AC-06). Static
text assertions, no AWS and no terraform binary.
"""

from __future__ import annotations

import re
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = GATEWAY_ROOT.parents[1]
MEMORY_TF = GATEWAY_ROOT / "infra" / "agentcore-memory.tf"
AGENTAUTH = GATEWAY_ROOT / "src" / "agentauth"
DEV_TFVARS = REPO_ROOT / "environments" / "dev" / "modules" / "gateway.tfvars"

COUNT_GATE = re.compile(r"^\s*count\s*=\s*local\.agentcore_memory_count\s*$", re.MULTILINE)
NAMESPACE_TEMPLATE = "/adp/owners/{actorId}"


def _blocks(kind: str) -> dict[str, str]:
    """Top-level ``<kind> "type" "name" { ... }`` (or ``<kind> "name" { ... }``) blocks.

    Keyed by ``type.name`` for resources/data sources and by ``name`` for
    variables/outputs. A top-level block closes at the first unindented ``}``.
    """
    text = MEMORY_TF.read_text()
    blocks: dict[str, str] = {}
    for match in re.finditer(rf'^{kind} "([^"]+)"(?: "([^"]+)")? \{{\n', text, re.MULTILINE):
        start = match.end()
        end = text.index("\n}\n", start)
        address = match.group(1) if match.group(2) is None else f"{match.group(1)}.{match.group(2)}"
        blocks[address] = text[start:end]
    assert blocks, f"no {kind} blocks found in {MEMORY_TF}"
    return blocks


def _block(kind: str, address: str) -> str:
    blocks = _blocks(kind)
    assert address in blocks, f"{address} missing from {MEMORY_TF}; have {sorted(blocks)}"
    return blocks[address]


def _agentcore_grant() -> str:
    """The gateway's data-plane statement list (a local consumed by gateway-role-policies.tf)."""
    text = MEMORY_TF.read_text()
    start = text.index("gateway_agentcore_memory_statements = ")
    end = text.index("] : []", start)
    return text[start:end]


def _code_data_plane_actions() -> set[str]:
    """IAM actions implied by every bedrock-agentcore client call in the gateway code."""
    methods: set[str] = set()
    for path in sorted(AGENTAUTH.glob("agentcore_*.py")):
        methods.update(re.findall(r"\bclient\.([a-z_]+)\(", path.read_text()))
    assert methods, "no bedrock-agentcore client calls found; the oracle moved"
    return {"bedrock-agentcore:" + "".join(part.title() for part in method.split("_")) for method in methods}


class TestEverythingIsBehindTheSwitch:
    def test_the_switch_defaults_off(self):
        variable = _block("variable", "agentcore_memory_enabled")
        assert re.search(r"^\s*default\s*=\s*false\s*$", variable, re.MULTILINE), "agentcore_memory_enabled must default to false"

    def test_the_gate_is_the_switch(self):
        assert "agentcore_memory_count = var.agentcore_memory_enabled ? 1 : 0" in MEMORY_TF.read_text()

    def test_every_resource_and_data_source_is_count_gated(self):
        ungated = [address for kind in ("resource", "data") for address, body in _blocks(kind).items() if not COUNT_GATE.search(body)]
        assert not ungated, f"created even when agentcore_memory_enabled is false: {ungated}"

    def test_outputs_are_null_when_disabled(self):
        for address, body in _blocks("output").items():
            assert re.search(r"value\s*=\s*try\(.*\[0\].*, null\)", body), f"{address} must be try(...[0], null) so a disabled environment plans"


class TestTheGatewayGrantMatchesTheCode:
    def test_policy_actions_are_exactly_the_calls_the_code_makes(self):
        policy = _agentcore_grant()
        declared = set(re.findall(r'"(bedrock-agentcore:[A-Za-z]+)"', policy))
        expected = _code_data_plane_actions()
        assert declared == expected, f"policy {sorted(declared)} != code {sorted(expected)}"
        assert expected == {
            "bedrock-agentcore:CreateEvent",
            "bedrock-agentcore:RetrieveMemoryRecords",
            "bedrock-agentcore:ListMemoryRecords",
            "bedrock-agentcore:GetMemoryRecord",
            "bedrock-agentcore:DeleteMemoryRecord",
        }, "the code's bedrock-agentcore call set changed; review the grant and this pin together"

    def test_policy_has_no_wildcard_action_or_resource(self):
        policy = _agentcore_grant()
        assert '"bedrock-agentcore:*"' not in policy
        assert not re.search(r'Resource\s*=\s*"\*"', policy)
        assert "aws_bedrockagentcore_memory.gateway_user_memory[0].arn" in policy, "the grant must be scoped to the memory ARN"

    def test_policy_attaches_to_the_gateway_service_role(self):
        """The grant is switch-gated and rides in the consolidated agents managed policy (#7944)."""
        policy = _agentcore_grant()
        assert policy.startswith("gateway_agentcore_memory_statements = local.agentcore_memory_count == 1 ? [")
        role_policies = (MEMORY_TF.parent / "gateway-role-policies.tf").read_text()
        start = role_policies.index("gateway_role_agents_statements = concat(")
        agents = role_policies[start : role_policies.index("gateway_role_policy_statements = {")]
        assert "local.gateway_agentcore_memory_statements," in agents
        assert 'resource "aws_iam_role_policy_attachment" "gateway_role"' in role_policies


class TestTheStrategyMatchesOwnerNamespaces:
    def test_code_derives_owner_namespace_under_the_template_prefix(self):
        recall = (AGENTAUTH / "agentcore_recall.py").read_text()
        prefix = NAMESPACE_TEMPLATE.split("{")[0]
        assert f'f"{prefix}{{digest}}"' in recall, "owner_namespace() moved; update the strategy template with it"

    def test_strategy_uses_one_namespace_per_actor(self):
        strategy = _block("resource", "aws_bedrockagentcore_memory_strategy.gateway_user_memory")
        whole_file = MEMORY_TF.read_text()
        assert f'agentcore_memory_namespace_owner = "{NAMESPACE_TEMPLATE}"' in whole_file
        assert "namespace_templates = [local.agentcore_memory_namespace_owner]" in strategy
        assert "{sessionId}" not in whole_file, "a per-session namespace breaks owner-scoped recall"

    def test_strategy_type_is_restricted_to_owner_scoped_kinds(self):
        variable = _block("variable", "agentcore_memory_strategy_type")
        assert 'contains(["SEMANTIC", "USER_PREFERENCE"], var.agentcore_memory_strategy_type)' in variable
        assert "SUMMARIZATION" not in re.sub(r"description\s*=.*", "", variable)


class TestTheNamespaceKeyIsASecret:
    def test_key_is_random_bytes_of_at_least_32(self):
        key = _block("resource", "random_bytes.agentcore_namespace_key")
        length = int(re.search(r"length\s*=\s*(\d+)", key).group(1))
        assert length >= 32, "agentcore_recall() rejects a key shorter than 32 bytes"

    def test_parameter_is_a_kms_encrypted_securestring_from_the_random_value(self):
        param = _block("resource", "aws_ssm_parameter.agentcore_namespace_key")
        assert 'type        = "SecureString"' in param
        assert "key_id      = aws_kms_key.secrets.arn" in param
        assert "value       = random_bytes.agentcore_namespace_key[0].hex" in param
        assert "/gateway/agentcore-namespace-key" in param

    def test_the_plain_parameters_carry_no_key_material(self):
        for name in ("agentcore_memory_id", "agentcore_memory_strategy_id", "agentcore_memory_region"):
            param = _block("resource", f"aws_ssm_parameter.{name}")
            assert 'type        = "String"' in param
            assert "random_bytes" not in param

    def test_region_parameter_is_the_provider_region(self):
        param = _block("resource", "aws_ssm_parameter.agentcore_memory_region")
        assert "value       = var.aws_region" in param, "the gateway refuses a memory region that differs from its AWS_REGION"


class TestTheDevSwitchIsStillOff:
    def test_dev_tfvars_mentions_the_switch_but_leaves_it_commented(self):
        lines = DEV_TFVARS.read_text().splitlines()
        mentions = [line for line in lines if "agentcore_memory_enabled" in line]
        assert mentions, "the operator switch must be documented in the dev tfvars"
        active = [line for line in mentions if not line.lstrip().startswith("#")]
        assert not active, f"dev must not enable AgentCore memory from the committed file: {active}"
