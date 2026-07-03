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


def test_model_and_provider_match_spike_2703() -> None:
    cfg = _load()
    assert cfg["model"] == "openai.gpt-5.5"
    assert cfg["model_provider"] == "amazon-bedrock"


def test_region_is_us_east_1() -> None:
    # bedrock-mantle serves gpt-5.5 in us-east-1 only.
    cfg = _load()
    assert cfg["model_providers"]["amazon-bedrock"]["aws"]["region"] == "us-east-1"


def test_approval_policy_is_never_for_headless() -> None:
    # A non-"never" policy would block a TTY-less KEDA pod on approval.
    cfg = _load()
    assert cfg["approval_policy"] == "never"
