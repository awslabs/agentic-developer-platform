"""Guards the EFFECTIVE chat-transcript redaction level (issue #5672).

Background. The application's own default scrub level is `standard` — headers,
regex patterns, and Amazon Comprehend PII detection, which is the only layer that
covers person names and free-form postal addresses. Both deploy paths overrode it
with the literal `basic` when rendering the cluster ConfigMap:

    -e "s|__CHAT_LOGGING_SCRUB_LEVEL__|basic|g"

So in every environment this repository shipped, customer content pasted into a
prompt was written to durable S3 storage exactly as typed, while the source default
still read `standard`.

That gap is the reason these tests exist and the reason they read the DEPLOY
ARTEFACTS rather than `ChatLoggingSettings`. A test over the application default
would have passed throughout the entire exposure: the source default was never
wrong. Only the rendered result was. Tests that assert on defaults cannot catch a
pipeline that overrides them.

These are text assertions over the workflow and the deploy script because the
substitution is textual, and because rendering for real needs a cluster, AWS
credentials and populated SSM parameters.
"""

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "gateway-deploy.yml"
_DEPLOY_ALL = _REPO_ROOT / "platform" / "scripts" / "deploy-all.sh"
_CONFIGMAP = _REPO_ROOT / "modules" / "gateway" / "k8s" / "configmap.yaml"

_PLACEHOLDER = "__CHAT_LOGGING_SCRUB_LEVEL__"
_STRONGEST = "standard"
_WEAKER_LEVELS = ("basic", "off", "none")

# Every file that renders the scrub level into a deployed ConfigMap. A new deploy
# path added here without being added to this list is the failure mode that let the
# original defect sit in two places at once.
_RENDERING_PATHS = [_WORKFLOW, _DEPLOY_ALL]


def _strip_shell_comments(text: str) -> str:
    """Drop whole-line `#` comments so prose naming a weak level never matches as code."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _substitution_values(text: str) -> list[str]:
    """Return every value substituted for the scrub-level placeholder."""
    return [m.group(1).strip() for m in re.finditer(rf"s\|{re.escape(_PLACEHOLDER)}\|([^|]*)\|", text)]


@pytest.mark.parametrize("path", _RENDERING_PATHS, ids=lambda p: p.name)
class TestNoDeployPathHardCodesAWeakerLevel:
    """The exact regression: a literal weak level in a deploy pipeline."""

    def test_substituted_value_is_not_a_hard_coded_weak_literal(self, path: Path):
        values = _substitution_values(_strip_shell_comments(path.read_text()))
        assert values, f"{path.name} no longer substitutes {_PLACEHOLDER} — did the render move?"

        for value in values:
            assert value.lower() not in _WEAKER_LEVELS, (
                f"{path.name} hard-codes the scrub level as {value!r}. That was issue #5672: "
                f"it silently overrode the application default of {_STRONGEST!r} in every "
                "environment, so personal data in prompts was stored as typed. A weaker level "
                "must be a per-environment parameter with a review trail, not a literal here."
            )

    def test_substituted_value_is_a_variable_reference(self, path: Path):
        """A variable can be overridden per environment; a literal cannot."""
        values = _substitution_values(_strip_shell_comments(path.read_text()))
        for value in values:
            assert "$" in value, (
                f"{path.name} substitutes the fixed value {value!r}. The level must come from a "
                "per-environment variable so an environment that needs something weaker makes "
                "that choice explicitly and reviewably."
            )

    def test_effective_default_is_the_strongest_level(self, path: Path):
        """An environment that says nothing must get full protection.

        This is the invariant that matters for a fresh account or a new environment,
        where the SSM parameter does not exist yet.
        """
        code = _strip_shell_comments(path.read_text())
        assignments = re.findall(r"CHAT_LOGGING_SCRUB_LEVEL=(.+)", code)
        assert assignments, f"{path.name} never assigns CHAT_LOGGING_SCRUB_LEVEL"

        joined = " ".join(assignments)
        assert _STRONGEST in joined, (
            f"{path.name} does not fall back to {_STRONGEST!r} when the per-environment "
            "parameter is unset. An environment with no explicit setting — including every "
            "new one — must get the strongest redaction, not the weakest."
        )

        for weak in _WEAKER_LEVELS:
            assert not re.search(rf"CHAT_LOGGING_SCRUB_LEVEL=[\"']?{weak}[\"']?\s*$", code, re.MULTILINE), (
                f"{path.name} assigns CHAT_LOGGING_SCRUB_LEVEL={weak!r} unconditionally."
            )


class TestConfigMapDoesNotPinAWeakLevel:
    def test_configmap_uses_the_placeholder_not_a_literal(self):
        """The ConfigMap must defer to the render, which defers to the environment."""
        code = _CONFIGMAP.read_text()
        match = re.search(r"BG_CHAT_LOGGING_SCRUB_LEVEL:\s*\"?([^\"\n]*)\"?", code)
        assert match, "BG_CHAT_LOGGING_SCRUB_LEVEL is missing from the gateway ConfigMap"

        value = match.group(1).strip()
        assert value == _PLACEHOLDER, (
            f"The ConfigMap pins BG_CHAT_LOGGING_SCRUB_LEVEL to {value!r}. It must stay the "
            f"{_PLACEHOLDER} placeholder so the value comes from the per-environment parameter. "
            f"A literal here would reintroduce issue #5672 one layer down."
        )


class TestTerraformDefaultMatchesTheCodeDefault:
    """The IAM grant for Comprehend is conditional on this variable.

    modules/gateway/infra/main.tf creates the comprehend:DetectPiiEntities policy
    only when chat_logging_scrub_level == "standard". If that default drifted below
    the rendered level, the workload would be configured for standard redaction but
    denied the API call — the silent-degradation case #5672 also addresses.
    """

    def test_infra_default_is_the_strongest_level(self):
        variables_tf = (_REPO_ROOT / "modules" / "gateway" / "infra" / "variables.tf").read_text()
        block_start = variables_tf.index('variable "chat_logging_scrub_level"')
        block = variables_tf[block_start : block_start + 800]

        match = re.search(r"default\s*=\s*\"([^\"]+)\"", block)
        assert match, "chat_logging_scrub_level has no explicit default in gateway infra variables"
        assert match.group(1) == _STRONGEST, (
            f"chat_logging_scrub_level defaults to {match.group(1)!r} in Terraform. It must be "
            f"{_STRONGEST!r}: the Comprehend IAM grant is conditional on this value, so a weaker "
            "default means the workload is configured for PII detection it cannot perform."
        )
