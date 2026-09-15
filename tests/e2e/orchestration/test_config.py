"""Config validation tests (#5156).

Proves the config layer refuses the unsafe input the issue enumerates rather
than defaulting it: missing bounds, embedded secrets, unknown targets and
malformed pinned versions. Network-free.
"""

from __future__ import annotations

import pytest

from tests.e2e.orchestration.config import (
    BOUND_CEILINGS,
    ConfigError,
    load_config,
    resolve_secret_ref,
)


def _problems(write_config, overrides, **kwargs) -> str:
    """Load a deliberately broken config and return its problems as one string."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(overrides, **kwargs))
    return "\n".join(excinfo.value.problems)


class TestValidConfig:
    def test_valid_config_loads_with_every_field(self, valid_config, artifact_dir):
        """The happy path parses into the typed accessors the harness uses."""
        assert valid_config.environment == "dev"
        assert valid_config.repository == "aws-e/adp"
        assert valid_config.connection_ref == "adp-dev-embark1"
        assert valid_config.org_ref == "qual-org"
        assert valid_config.versions["engine"] == "1.4.2"
        assert valid_config.max_resources == 5
        assert valid_config.max_runs == 2
        assert valid_config.max_usd == pytest.approx(5.0)
        assert valid_config.max_duration_seconds == 900
        assert valid_config.artifact_directory == artifact_dir

    def test_config_holds_secret_references_not_secrets(self, valid_config):
        """A config carries a POINTER to a credential, never the credential."""
        assert valid_config.secret_refs == {"github_app_key": "secretsmanager:adp/dev/github-app/private-key"}

    def test_ownership_tags_bind_a_fixture_to_one_qualification(self, valid_config):
        """Tags are what cleanup later uses to prove a fixture is ours."""
        tags = valid_config.ownership_tags("q-abc123def456")
        assert tags["adp:qualification-id"] == "q-abc123def456"
        assert tags["adp:qualification-environment"] == "dev"
        assert tags["adp:managed-by"] == "tests.e2e.orchestration"

    def test_loading_makes_no_aws_call(self, write_config, monkeypatch):
        """Loading must work with no credentials: --preflight depends on it."""
        import boto3

        def explode(*args, **kwargs):
            raise AssertionError("load_config must not construct an AWS client")

        monkeypatch.setattr(boto3, "client", explode)
        assert load_config(write_config()).environment == "dev"


class TestMissingBounds:
    @pytest.mark.parametrize("bound", sorted(BOUND_CEILINGS))
    def test_each_missing_bound_is_rejected(self, write_config, bound):
        """An absent cap is refused; it never silently becomes unlimited."""
        with pytest.raises(ConfigError) as excinfo:
            load_config(write_config(remove=[f"bounds.{bound}"]))
        assert f"missing required key: bounds.{bound}" in "\n".join(excinfo.value.problems)

    def test_bounds_section_entirely_missing_is_rejected(self, write_config):
        with pytest.raises(ConfigError) as excinfo:
            load_config(write_config(remove=["bounds"]))
        assert "missing required key: bounds" in "\n".join(excinfo.value.problems)

    @pytest.mark.parametrize("value", [0, -1, -0.5])
    def test_non_positive_bound_is_rejected(self, write_config, value):
        """A zero or negative cap would make the run meaningless or unbounded."""
        problems = _problems(write_config, {"bounds": {"max_runs": value}})
        assert "bounds.max_runs must be greater than zero" in problems

    @pytest.mark.parametrize("value", ["unlimited", None, True, [], {}])
    def test_non_numeric_bound_is_rejected(self, write_config, value):
        problems = _problems(write_config, {"bounds": {"max_usd": value}})
        assert "bounds.max_usd must be a positive number" in problems

    def test_infinite_bound_is_rejected(self, write_config):
        """`Infinity` is valid JSON to Python's parser but not a bound."""
        problems = _problems(write_config, {"bounds": {"max_usd": float("inf")}})
        assert "bounds.max_usd must be finite" in problems

    def test_bound_over_the_harness_ceiling_is_rejected(self, write_config):
        """A typo'd 10000 USD cap is refused before it can be spent."""
        problems = _problems(write_config, {"bounds": {"max_usd": 10_000}})
        assert "exceeds the harness ceiling" in problems

    def test_fractional_resource_count_is_rejected(self, write_config):
        problems = _problems(write_config, {"bounds": {"max_resources": 2.5}})
        assert "bounds.max_resources must be a whole number" in problems


class TestEmbeddedSecrets:
    def test_secret_like_key_anywhere_is_rejected(self, write_config):
        """A pasted credential is refused by the KEY name, whatever its value."""
        problems = _problems(write_config, {"connection": {"password": "hunter2"}})
        assert "secret-like key is not allowed" in problems

    @pytest.mark.parametrize(
        "value",
        [
            "AKIAIOSFODNN7EXAMPLE",
            "ASIAIOSFODNN7EXAMPLE",
            "ghp_" + "a" * 36,
            "github_pat_" + "b" * 30,
            "-----BEGIN RSA PRIVATE KEY-----",
            "xoxb-123456789012-abcdefghijkl",
        ],
    )
    def test_credential_shaped_value_is_rejected(self, write_config, value):
        """Credential SHAPES are caught even under an innocent key name."""
        problems = _problems(write_config, {"identity": {"org_ref": value}})
        assert "looks like an embedded" in problems

    def test_rejection_never_echoes_the_secret(self, write_config):
        """An error message is logged; it must not leak what it caught."""
        secret = "ghp_" + "z" * 36
        with pytest.raises(ConfigError) as excinfo:
            load_config(write_config({"identity": {"team_ref": secret}}))
        assert secret not in str(excinfo.value)
        assert secret not in "\n".join(excinfo.value.problems)

    def test_secret_ref_section_permits_reference_names(self, write_config):
        """`secret_refs` is the one place a secret-like NAME is expected."""
        config = load_config(write_config({"secret_refs": {"api_key": "ssm:/adp/dev/qual/key"}}))
        assert config.secret_refs["api_key"] == "ssm:/adp/dev/qual/key"

    def test_literal_value_in_secret_refs_is_rejected(self, write_config):
        """Even in `secret_refs`, a literal is refused: it must be a reference."""
        problems = _problems(write_config, {"secret_refs": {"github_app_key": "ghp_" + "c" * 36}})
        assert "must be a reference of the form" in problems

    def test_malformed_secret_reference_scheme_is_rejected(self, write_config):
        problems = _problems(write_config, {"secret_refs": {"k": "vault:/some/path"}})
        assert "must be a reference of the form" in problems


class TestUnknownTargets:
    def test_unknown_environment_is_rejected(self, write_config):
        problems = _problems(write_config, {"environment": "qa-sandbox"})
        assert "unknown target environment" in problems

    @pytest.mark.parametrize("environment", ["prod", "production"])
    def test_production_target_is_refused(self, write_config, environment):
        """This harness provisions fixtures; production is never a target."""
        problems = _problems(write_config, {"environment": environment})
        assert "not authorized against production" in problems

    def test_unknown_top_level_key_is_rejected(self, write_config):
        """A typo'd key must not be ignored: it may be a bound that never applies."""
        problems = _problems(write_config, {"max_spend": 100})
        assert "unknown top-level key(s): max_spend" in problems

    def test_unknown_section_key_is_rejected(self, write_config):
        problems = _problems(write_config, {"bounds": {"max_tokens": 10}})
        assert "unknown key(s) in bounds: max_tokens" in problems

    def test_malformed_repository_is_rejected(self, write_config):
        problems = _problems(write_config, {"connection": {"repository": "not-a-repo"}})
        assert "must be 'owner/repo'" in problems

    def test_unknown_scenario_id_shape_is_rejected(self, write_config):
        problems = _problems(write_config, {"scenarios": ["Not A Scenario!"]})
        assert "scenarios[0] must be a scenario adapter id" in problems


class TestPinnedVersions:
    @pytest.mark.parametrize("floating", ["latest", "main", "master", "HEAD", "stable", "*", ""])
    def test_floating_version_is_rejected(self, write_config, floating):
        """A floating ref makes a qualification unreproducible."""
        problems = _problems(write_config, {"versions": {"engine": floating}})
        assert "must be pinned" in problems

    @pytest.mark.parametrize("malformed", ["1.2", "v1.2.3", "1.2.3.4", "abc123", "1.2.x"])
    def test_malformed_version_is_rejected(self, write_config, malformed):
        problems = _problems(write_config, {"versions": {"worker": malformed}})
        assert "is malformed" in problems

    @pytest.mark.parametrize("pinned", ["1.2.3", "0.0.1", "2.0.0-rc.1", "a" * 40])
    def test_exact_semver_or_commit_sha_is_accepted(self, write_config, pinned):
        assert load_config(write_config({"versions": {"engine": pinned}})).versions["engine"] == pinned

    def test_missing_version_is_rejected(self, write_config):
        with pytest.raises(ConfigError) as excinfo:
            load_config(write_config(remove=["versions.harness"]))
        assert "missing required key: versions.harness" in "\n".join(excinfo.value.problems)


class TestArtifactDirectory:
    def test_traversal_in_artifact_directory_is_rejected(self, write_config):
        """`..` in the artifact path could write evidence outside the run."""
        problems = _problems(write_config, {"artifacts": {"directory": "../../etc/adp"}})
        assert "must not contain '..'" in problems

    def test_empty_artifact_directory_is_rejected(self, write_config):
        problems = _problems(write_config, {"artifacts": {"directory": "   "}})
        assert "must be a non-empty path" in problems


class TestFileLevelErrors:
    def test_missing_file_is_reported_clearly(self, tmp_path):
        with pytest.raises(ConfigError) as excinfo:
            load_config(tmp_path / "nope.json")
        assert "does not exist" in str(excinfo.value)

    def test_invalid_json_is_reported_as_such(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "not valid JSON" in str(excinfo.value)

    def test_non_object_root_is_rejected(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(path)

    def test_wrong_config_version_is_rejected(self, write_config):
        problems = _problems(write_config, {"config_version": 99})
        assert "config_version must be 1" in problems

    def test_secret_refs_section_is_optional(self, write_config):
        """A qualification needing no secret reference is still valid."""
        assert load_config(write_config(remove=["secret_refs"])).secret_refs == {}

    def test_every_problem_is_reported_at_once(self, write_config):
        """An operator fixes the file in one pass, not one error per run."""
        problems = _problems(
            write_config,
            {
                "environment": "nowhere",
                "versions": {"engine": "latest"},
                "bounds": {"max_usd": -1},
            },
        )
        assert "unknown target environment" in problems
        assert "must be pinned" in problems
        assert "greater than zero" in problems


class TestSecretResolution:
    def test_env_reference_resolves_at_runtime(self, monkeypatch):
        monkeypatch.setenv("QUAL_TEST_SECRET", "resolved-value")
        assert resolve_secret_ref("env:QUAL_TEST_SECRET") == "resolved-value"

    def test_absent_env_reference_fails_loudly(self, monkeypatch):
        """A missing credential must stop the run, not yield an empty string."""
        monkeypatch.delenv("QUAL_TEST_ABSENT", raising=False)
        with pytest.raises(ConfigError) as excinfo:
            resolve_secret_ref("env:QUAL_TEST_ABSENT")
        assert "not set in the environment" in str(excinfo.value)

    def test_invalid_reference_is_refused_before_any_client_is_built(self):
        with pytest.raises(ConfigError):
            resolve_secret_ref("not-a-reference")
