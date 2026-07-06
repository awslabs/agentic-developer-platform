"""Config-shape guard for codex-config.toml (issue #2704).

The issue's impact table calls out "Bad config.toml" as a bug class that makes
every `codex` invocation fail at start. There's no runtime code to unit-test
(it's a baked-in static file), so this test parses the committed config and
asserts the settled design values from spike #2703 so a typo can't slip
through review.
"""

from __future__ import annotations

import os
import tomllib

CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "codex-config.toml"
)


def _load() -> dict:
    with open(CONFIG_PATH, "rb") as fh:
        return tomllib.load(fh)


def test_config_is_valid_toml() -> None:
    # Parsing failure here == the pod-hang bug class from the impact table.
    assert isinstance(_load(), dict)


def test_model_is_gpt55() -> None:
    cfg = _load()
    assert cfg["model"] == "openai.gpt-5.5"


def test_provider_is_gateway_after_cutover_2713() -> None:
    # v2 cutover (#2713): default provider must be the custom gateway provider
    # so Codex traffic rides the sigv4-proxy sidecar → gateway metering, not the
    # direct-IRSA amazon-bedrock path.
    cfg = _load()
    assert cfg["model_provider"] == "adp-gateway"


def test_gateway_provider_points_at_sidecar_responses_path() -> None:
    # base_url must end at /openai/v1 so Codex (wire_api=responses) POSTs to
    # exactly /openai/v1/responses at the gateway pod (verified on 0.142.5).
    cfg = _load()
    gw = cfg["model_providers"]["adp-gateway"]
    assert gw["base_url"] == "http://127.0.0.1:9090/openai/v1"
    assert gw["wire_api"] == "responses"


def test_gateway_provider_uses_placeholder_env_key_not_a_secret() -> None:
    # SigV4-only invariant (#2713): the provider names an env var whose value is
    # a placeholder (sidecar re-signs). It must NOT be a real key/secret ref.
    cfg = _load()
    assert cfg["model_providers"]["adp-gateway"]["env_key"] == "ADP_GATEWAY_PLACEHOLDER_KEY"


def test_amazon_bedrock_fallback_present_but_not_default() -> None:
    # #2713 I3: keep the built-in provider as the soak-window rollback path,
    # present-but-non-default. Region stays us-east-1 (mantle is us-east-1 only).
    cfg = _load()
    assert cfg["model_provider"] != "amazon-bedrock"
    assert cfg["model_providers"]["amazon-bedrock"]["aws"]["region"] == "us-east-1"


def test_approval_policy_is_never_for_headless() -> None:
    # A non-"never" policy would block a TTY-less KEDA pod on approval.
    cfg = _load()
    assert cfg["approval_policy"] == "never"
