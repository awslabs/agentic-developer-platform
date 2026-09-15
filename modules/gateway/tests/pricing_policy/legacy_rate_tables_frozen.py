"""Frozen copies of the three pre-#4976 rate literals (issue #4969, design §7).

**This file is a historical record. Do not "update" it.**

Before this release the gateway carried three independently hand-maintained rate
tables — ``src/budget/pricing.py``, ``lambda/shared/pricing_fallback.py`` and
``src/budget/config.py`` — because no import path exists between ``src/`` and
``lambda/``. This release collapses all three onto the shared ``pricing_policy``
snapshot. The values below are those tables as they stood at the commit before
the collapse, copied verbatim by a one-off script.

They exist so the collapse can be proven ledger-neutral for non-OpenAI traffic.
Comparing the new runtime tables against each other would be circular — they are
now all derived from the same snapshot, so they cannot disagree. Only a frozen
copy of what actually billed beforehand can show that nothing moved.

If a future release legitimately changes a non-OpenAI rate, the parity test that
reads this file should gain an explicit, per-model exception naming the reason.
Editing the numbers here instead would erase the evidence that the rate changed
at all — which is the failure mode #4969 was filed about.

OpenAI rates are deliberately NOT asserted for parity: correcting them is the
point of #4969. See §10 of the design note for the published-versus-bundled
inventory (Luna was billing 5.00x its published rate).
"""

from decimal import Decimal

#: ``src/budget/pricing.py::MODEL_PRICING`` before the collapse. Priced the
#: gateway's pre-request enforcement estimate and the mantle ``usage_logs`` row.
GATEWAY_MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    "global.anthropic.claude-opus-5": {"input": Decimal("0.005"), "output": Decimal("0.025")},
    "anthropic.claude-opus-5": {"input": Decimal("0.005"), "output": Decimal("0.025")},
    "global.anthropic.claude-opus-4-8": {"input": Decimal("0.005"), "output": Decimal("0.025")},
    "global.anthropic.claude-opus-4-7": {"input": Decimal("0.005"), "output": Decimal("0.025")},
    "global.anthropic.claude-opus-4-6-v1": {"input": Decimal("0.005"), "output": Decimal("0.025")},
    "global.anthropic.claude-opus-4-5-20251101-v1:0": {"input": Decimal("0.005"), "output": Decimal("0.025")},
    "global.anthropic.claude-sonnet-4-6": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "global.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": Decimal("0.001"), "output": Decimal("0.005")},
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "anthropic.claude-3-5-haiku-20241022-v1:0": {"input": Decimal("0.0008"), "output": Decimal("0.004")},
    "anthropic.claude-3-opus-20240229-v1:0": {"input": Decimal("0.015"), "output": Decimal("0.075")},
    "anthropic.claude-3-sonnet-20240229-v1:0": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "anthropic.claude-3-haiku-20240307-v1:0": {"input": Decimal("0.00025"), "output": Decimal("0.00125")},
    "anthropic.claude-v2:1": {"input": Decimal("0.008"), "output": Decimal("0.024")},
    "anthropic.claude-v2": {"input": Decimal("0.008"), "output": Decimal("0.024")},
    "anthropic.claude-instant-v1": {"input": Decimal("0.0008"), "output": Decimal("0.0024")},
    "amazon.titan-text-express-v1": {"input": Decimal("0.0002"), "output": Decimal("0.0006")},
    "amazon.titan-text-lite-v1": {"input": Decimal("0.00015"), "output": Decimal("0.0002")},
    "amazon.titan-text-premier-v1:0": {"input": Decimal("0.0005"), "output": Decimal("0.0015")},
    "amazon.titan-embed-text-v1": {"input": Decimal("0.0001"), "output": Decimal("0")},
    "amazon.titan-embed-text-v2:0": {"input": Decimal("0.00002"), "output": Decimal("0")},
    "cohere.command-text-v14": {"input": Decimal("0.0015"), "output": Decimal("0.002")},
    "cohere.command-light-text-v14": {"input": Decimal("0.0003"), "output": Decimal("0.0006")},
    "cohere.command-r-v1:0": {"input": Decimal("0.0005"), "output": Decimal("0.0015")},
    "cohere.command-r-plus-v1:0": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "meta.llama3-8b-instruct-v1:0": {"input": Decimal("0.0003"), "output": Decimal("0.0006")},
    "meta.llama3-70b-instruct-v1:0": {"input": Decimal("0.00265"), "output": Decimal("0.0035")},
    "meta.llama3-1-8b-instruct-v1:0": {"input": Decimal("0.00022"), "output": Decimal("0.00022")},
    "meta.llama3-1-70b-instruct-v1:0": {"input": Decimal("0.00099"), "output": Decimal("0.00099")},
    "meta.llama3-1-405b-instruct-v1:0": {"input": Decimal("0.00532"), "output": Decimal("0.016")},
    "meta.llama3-2-1b-instruct-v1:0": {"input": Decimal("0.0001"), "output": Decimal("0.0001")},
    "meta.llama3-2-3b-instruct-v1:0": {"input": Decimal("0.00015"), "output": Decimal("0.00015")},
    "meta.llama3-2-11b-instruct-v1:0": {"input": Decimal("0.00016"), "output": Decimal("0.00016")},
    "meta.llama3-2-90b-instruct-v1:0": {"input": Decimal("0.00072"), "output": Decimal("0.00072")},
    "mistral.mistral-7b-instruct-v0:2": {"input": Decimal("0.00015"), "output": Decimal("0.0002")},
    "mistral.mixtral-8x7b-instruct-v0:1": {"input": Decimal("0.00045"), "output": Decimal("0.0007")},
    "mistral.mistral-large-2402-v1:0": {"input": Decimal("0.004"), "output": Decimal("0.012")},
    "mistral.mistral-small-2402-v1:0": {"input": Decimal("0.001"), "output": Decimal("0.003")},
    "ai21.j2-ultra-v1": {"input": Decimal("0.0125"), "output": Decimal("0.0125")},
    "ai21.j2-mid-v1": {"input": Decimal("0.0125"), "output": Decimal("0.0125")},
    "openai.gpt-5.5": {"input": Decimal("0.0055"), "output": Decimal("0.033")},
    "openai.gpt-5.6-sol": {"input": Decimal("0.0055"), "output": Decimal("0.033")},
    "openai.gpt-5.6-terra": {"input": Decimal("0.00275"), "output": Decimal("0.0165")},
    "openai.gpt-5.6-luna": {"input": Decimal("0.0011"), "output": Decimal("0.0066")},
    "openai.gpt-oss-120b": {"input": Decimal("0.0001545"), "output": Decimal("0.000618")},
    "default": {"input": Decimal("0.003"), "output": Decimal("0.015")},
}

#: ``src/budget/pricing.py::MODEL_ALIASES`` before the collapse.
GATEWAY_MODEL_ALIASES: dict[str, str] = {
    "opus5": "global.anthropic.claude-opus-5",
    "claude-opus-5": "global.anthropic.claude-opus-5",
    "opus48": "global.anthropic.claude-opus-4-8",
    "opus47": "global.anthropic.claude-opus-4-7",
    "opus46": "global.anthropic.claude-opus-4-6-v1",
    "sonnet46": "global.anthropic.claude-sonnet-4-6",
    "haiku45": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-3-5-sonnet": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-sonnet-20241022": "anthropic.claude-3-5-sonnet-20241022-v2:0",
    "claude-3-5-haiku": "anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-3-5-haiku-20241022": "anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-3-opus": "anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-opus-20240229": "anthropic.claude-3-opus-20240229-v1:0",
    "claude-3-sonnet": "anthropic.claude-3-sonnet-20240229-v1:0",
    "claude-3-sonnet-20240229": "anthropic.claude-3-sonnet-20240229-v1:0",
    "claude-3-haiku": "anthropic.claude-3-haiku-20240307-v1:0",
    "claude-3-haiku-20240307": "anthropic.claude-3-haiku-20240307-v1:0",
    "claude-2.1": "anthropic.claude-v2:1",
    "claude-2": "anthropic.claude-v2",
    "claude-instant-1.2": "anthropic.claude-instant-v1",
}

#: ``lambda/shared/pricing_fallback.py::MODEL_PRICING`` before the collapse. This
#: is the settlement authority: its values wrote ``budget_usage.total_cost_usd``
#: and ``usage_logs.cost_usd``, so it is the table parity matters most against.
SETTLEMENT_MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
        "cache_read_input": Decimal("0.0003"),
        "cache_creation_input": Decimal("0.00375"),
    },
    "anthropic.claude-3-5-haiku-20241022-v1:0": {
        "input": Decimal("0.0008"),
        "output": Decimal("0.004"),
        "cache_read_input": Decimal("0.00008"),
        "cache_creation_input": Decimal("0.001"),
    },
    "anthropic.claude-opus-4-20250514-v1:0": {
        "input": Decimal("0.015"),
        "output": Decimal("0.075"),
        "cache_read_input": Decimal("0.0015"),
        "cache_creation_input": Decimal("0.01875"),
    },
    "anthropic.claude-sonnet-4-20250514-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
        "cache_read_input": Decimal("0.0003"),
        "cache_creation_input": Decimal("0.00375"),
    },
    "anthropic.claude-haiku-4-20250514-v1:0": {
        "input": Decimal("0.0008"),
        "output": Decimal("0.004"),
        "cache_read_input": Decimal("0.00008"),
        "cache_creation_input": Decimal("0.001"),
    },
    "anthropic.claude-opus-4-6-v1": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),
        "cache_creation_input": Decimal("0.00625"),
    },
    "anthropic.claude-opus-4-7-v1": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),
        "cache_creation_input": Decimal("0.00625"),
    },
    "anthropic.claude-opus-4-8-v1": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),
        "cache_creation_input": Decimal("0.00625"),
    },
    "anthropic.claude-sonnet-4-6-v1": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
        "cache_read_input": Decimal("0.0003"),
        "cache_creation_input": Decimal("0.00375"),
    },
    "anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
        "cache_read_input": Decimal("0.0003"),
        "cache_creation_input": Decimal("0.00375"),
    },
    "anthropic.claude-opus-5": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),
        "cache_creation_input": Decimal("0.00625"),
    },
    "anthropic.claude-opus-4-5-20251101-v1:0": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
        "cache_read_input": Decimal("0.0005"),
        "cache_creation_input": Decimal("0.00625"),
    },
    "anthropic.claude-haiku-4-5-20251001-v1:0": {
        "input": Decimal("0.0008"),
        "output": Decimal("0.004"),
        "cache_read_input": Decimal("0.00008"),
        "cache_creation_input": Decimal("0.001"),
    },
    "anthropic.claude-3-opus-20240229-v1:0": {"input": Decimal("0.015"), "output": Decimal("0.075")},
    "anthropic.claude-3-sonnet-20240229-v1:0": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "anthropic.claude-3-haiku-20240307-v1:0": {"input": Decimal("0.00025"), "output": Decimal("0.00125")},
    "anthropic.claude-v2:1": {"input": Decimal("0.008"), "output": Decimal("0.024")},
    "anthropic.claude-v2": {"input": Decimal("0.008"), "output": Decimal("0.024")},
    "anthropic.claude-instant-v1": {"input": Decimal("0.0008"), "output": Decimal("0.0024")},
    "amazon.titan-text-express-v1": {"input": Decimal("0.0002"), "output": Decimal("0.0006")},
    "amazon.titan-text-lite-v1": {"input": Decimal("0.00015"), "output": Decimal("0.0002")},
    "amazon.titan-text-premier-v1:0": {"input": Decimal("0.0005"), "output": Decimal("0.0015")},
    "amazon.titan-embed-text-v1": {"input": Decimal("0.0001"), "output": Decimal("0")},
    "amazon.titan-embed-text-v2:0": {"input": Decimal("0.00002"), "output": Decimal("0")},
    "cohere.command-text-v14": {"input": Decimal("0.0015"), "output": Decimal("0.002")},
    "cohere.command-light-text-v14": {"input": Decimal("0.0003"), "output": Decimal("0.0006")},
    "cohere.command-r-v1:0": {"input": Decimal("0.0005"), "output": Decimal("0.0015")},
    "cohere.command-r-plus-v1:0": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "meta.llama3-8b-instruct-v1:0": {"input": Decimal("0.0003"), "output": Decimal("0.0006")},
    "meta.llama3-70b-instruct-v1:0": {"input": Decimal("0.00265"), "output": Decimal("0.0035")},
    "meta.llama3-1-8b-instruct-v1:0": {"input": Decimal("0.00022"), "output": Decimal("0.00022")},
    "meta.llama3-1-70b-instruct-v1:0": {"input": Decimal("0.00099"), "output": Decimal("0.00099")},
    "meta.llama3-1-405b-instruct-v1:0": {"input": Decimal("0.00532"), "output": Decimal("0.016")},
    "meta.llama3-2-1b-instruct-v1:0": {"input": Decimal("0.0001"), "output": Decimal("0.0001")},
    "meta.llama3-2-3b-instruct-v1:0": {"input": Decimal("0.00015"), "output": Decimal("0.00015")},
    "meta.llama3-2-11b-instruct-v1:0": {"input": Decimal("0.00016"), "output": Decimal("0.00016")},
    "meta.llama3-2-90b-instruct-v1:0": {"input": Decimal("0.00072"), "output": Decimal("0.00072")},
    "mistral.mistral-7b-instruct-v0:2": {"input": Decimal("0.00015"), "output": Decimal("0.0002")},
    "mistral.mixtral-8x7b-instruct-v0:1": {"input": Decimal("0.00045"), "output": Decimal("0.0007")},
    "mistral.mistral-large-2402-v1:0": {"input": Decimal("0.004"), "output": Decimal("0.012")},
    "mistral.mistral-small-2402-v1:0": {"input": Decimal("0.001"), "output": Decimal("0.003")},
    "ai21.j2-ultra-v1": {"input": Decimal("0.0125"), "output": Decimal("0.0125")},
    "ai21.j2-mid-v1": {"input": Decimal("0.0125"), "output": Decimal("0.0125")},
    "openai.gpt-5.5": {"input": Decimal("0.0055"), "output": Decimal("0.033")},
    "openai.gpt-5.6-sol": {"input": Decimal("0.0055"), "output": Decimal("0.033")},
    "openai.gpt-5.6-terra": {"input": Decimal("0.00275"), "output": Decimal("0.0165")},
    "openai.gpt-5.6-luna": {"input": Decimal("0.0011"), "output": Decimal("0.0066")},
    "openai.gpt-oss-120b": {"input": Decimal("0.0001545"), "output": Decimal("0.000618")},
    "default": {"input": Decimal("0.003"), "output": Decimal("0.015")},
}

#: ``src/budget/config.py::BudgetConfig.model_pricing`` before the collapse.
#: Read only by ``src/budget/utils.py::calculate_model_cost``, and keyed by short
#: model names rather than Bedrock ids.
BUDGET_CONFIG_MODEL_PRICING: dict[str, dict[str, Decimal]] = {
    "claude-3-5-sonnet-20241022": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "claude-3-5-sonnet-20240620": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "claude-3-5-haiku-20241022": {"input": Decimal("0.0008"), "output": Decimal("0.004")},
    "claude-3-opus-20240229": {"input": Decimal("0.015"), "output": Decimal("0.075")},
    "claude-3-sonnet-20240229": {"input": Decimal("0.003"), "output": Decimal("0.015")},
    "claude-3-haiku-20240307": {"input": Decimal("0.00025"), "output": Decimal("0.00125")},
    "claude-2.1": {"input": Decimal("0.008"), "output": Decimal("0.024")},
    "claude-2.0": {"input": Decimal("0.008"), "output": Decimal("0.024")},
    "claude-instant-1.2": {"input": Decimal("0.0008"), "output": Decimal("0.0024")},
    "default": {"input": Decimal("0.003"), "output": Decimal("0.015")},
}
