"""An administrator handoff can only be resumed by the deployment that made it
(Issue #5413).

`adp aws connect --download` and `adp admin bedrock connect --download` exist for
the user who cannot create IAM resources themselves: the CLI writes a directory,
somebody with AWS access applies it, and the user comes back later with
`--resume`. "Later" is the problem. The directory outlives the command that wrote
it, so by the time it returns the saved default may name a different deployment —
and resuming it there would provision a role, or point an entire organization's
Bedrock routing, at an account nobody chose in that environment.

The existing gateway-URL check does not cover this. Two deployment records may be
ALIASES of one URL (same gateway, different local session), and a name can be
re-pointed while a handoff is in flight, so a URL is a binding rather than an
identity. The stamp recorded now is the stable deployment id.

Two properties are asserted, and the second is as important as the first:

1. a handoff carries whose it is, and a mismatched resume is refused;
2. the refusal happens BEFORE any mutation — no ADP write, no AWS call, no state
   overwritten. A late refusal would already have changed the thing it rejected.

Both providers are covered, because they are separate code paths with separate
metadata files, and a fix applied to only one is the bug still shipping.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
import adp_common as common  # noqa: E402

_spec = importlib.util.spec_from_file_location("adp_deployments_for_handoff", CLI / "adp_deployments.py")
deployments = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deployments)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name.replace('-', '_')}_for_handoff", CLI / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aws_cli = _load("adp-aws")
bedrock_cli = _load("adp-bedrock")


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """One machine with `dev` and `integration` registered, nothing selected yet.

    `adp_common` caches its resolution deliberately (one answer per process), so
    the cache is reset here and again on every `select` — otherwise the first
    selection in a test file would silently become every later one.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("ADP_HOME", str(home / ".adp"))
    for leaked in ("ADP_DEPLOYMENT", "ADP_DEPLOYMENT_ID", "ADP_DEPLOYMENT_NAME", "ADP_DEPLOYMENT_SOURCE", "BG_CONFIG_DIR"):
        monkeypatch.delenv(leaked, raising=False)
    monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)
    deployments.add("dev", "https://dev.example.com")
    deployments.add("integration", "https://integration.example.com")

    def select(name: str):
        monkeypatch.setenv("ADP_DEPLOYMENT", name)
        monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)
        return deployments.resolve(name)

    return select


# ---------------------------------------------------------------------------
# The stamp itself
# ---------------------------------------------------------------------------


class TestAHandoffRecordsWhoseItIs:
    def test_the_stamp_names_the_selected_deployment(self, machine):
        dev = machine("dev")

        stamp = common.deployment_stamp()

        assert stamp == {"deployment_id": dev.id, "deployment": "dev"}

    def test_the_stamp_carries_nothing_secret(self, machine):
        """It is written into a directory the user hands to an administrator."""
        machine("dev")

        assert set(common.deployment_stamp()) == {"deployment_id", "deployment"}

    def test_a_legacy_machine_stamps_nothing_to_mismatch(self, tmp_path, monkeypatch):
        """An existing single-deployment user has no registry, so their handoffs
        must behave exactly as they did before this change."""
        home = tmp_path / "legacy"
        (home / ".bedrock-gateway").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setenv("ADP_HOME", str(home / ".adp"))
        monkeypatch.setattr(common, "_deployment", common._UNRESOLVED, raising=False)

        stamp = common.deployment_stamp()

        common.check_handoff_deployment(stamp)  # its own handoff always resumes
        assert stamp == {"deployment_id": "", "deployment": ""}


class TestTheCheckAcceptsAndRefusesTheRightThings:
    def test_our_own_handoff_is_accepted(self, machine):
        machine("dev")
        stamp = common.deployment_stamp()

        common.check_handoff_deployment(stamp)  # must not raise

    def test_another_deployments_handoff_is_refused(self, machine):
        machine("dev")
        written_by_dev = common.deployment_stamp()
        machine("integration")

        with pytest.raises(common.CliError) as failure:
            common.check_handoff_deployment(written_by_dev)

        assert failure.value.code == "deployment_mismatch"

    def test_the_refusal_names_both_sides_and_how_to_fix_it(self, machine):
        """The user has a directory in their hand; the message must say which
        deployment it belongs to and what to type."""
        machine("dev")
        written_by_dev = common.deployment_stamp()
        machine("integration")

        with pytest.raises(common.CliError) as failure:
            common.check_handoff_deployment(written_by_dev, "AWS setup")

        message = str(failure.value)
        assert "dev" in message and "integration" in message
        assert "--deployment dev" in message
        assert "Nothing was changed" in message

    def test_an_alias_of_the_same_gateway_still_resumes(self, machine):
        """Two names for one URL are one session and one stable id, so a handoff
        written under either name is genuinely the same deployment's — and the
        gateway-URL check alone could not tell these two cases apart."""
        deployments.add("dev-alias", "https://dev.example.com")
        machine("dev")
        written_by_dev = common.deployment_stamp()

        machine("dev-alias")

        common.check_handoff_deployment(written_by_dev)  # must not raise

    def test_a_handoff_written_before_this_change_is_still_resumable(self, machine):
        """No stamp means an older CLI wrote it. Refusing would strand a setup the
        user is part-way through, and the gateway-URL check still applies to it."""
        machine("dev")

        common.check_handoff_deployment({"gateway_url": "https://dev.example.com/api"})
        common.check_handoff_deployment({})
        common.check_handoff_deployment(None)


# ---------------------------------------------------------------------------
# End to end through the two real providers
# ---------------------------------------------------------------------------


def _aws_api():
    from tests.cli.test_adp_aws import FakeApi

    return FakeApi()


def _bedrock_api():
    from tests.cli.test_adp_bedrock import FakeApi

    return FakeApi()


class TestAwsHandoffCrossesNoDeployment:
    def test_the_downloaded_metadata_carries_the_deployment(self, machine, tmp_path, monkeypatch):
        dev = machine("dev")
        api = _aws_api()
        monkeypatch.setattr(aws_cli, "Aws", lambda *args: pytest.fail("a download must not touch AWS"))
        directory = tmp_path / "handoff"

        aws_cli.run(aws_cli.parser().parse_args(["connect", "--account", "123456789012", "--yes", "--download", str(directory)]), api)

        metadata = json.loads((directory / "connection.json").read_text())
        assert metadata["deployment_id"] == dev.id
        assert metadata["deployment"] == "dev"

    def test_resuming_it_elsewhere_fails_before_anything_is_created(self, machine, tmp_path, monkeypatch):
        """The ordering claim: refused with zero writes to ADP and zero AWS calls."""
        machine("dev")
        api = _aws_api()
        monkeypatch.setattr(aws_cli, "Aws", lambda *args: pytest.fail("a refused resume must not touch AWS"))
        directory = tmp_path / "handoff"
        aws_cli.run(aws_cli.parser().parse_args(["connect", "--account", "123456789012", "--yes", "--download", str(directory)]), api)

        machine("integration")
        api.calls.clear()
        with pytest.raises(aws_cli.CliError) as failure:
            aws_cli.run(aws_cli.parser().parse_args(["connect", "--resume", str(directory), "--yes"]), api)

        assert failure.value.code == "deployment_mismatch"
        assert [call for call in api.calls if call[0] != "GET"] == [], "a refused resume still changed ADP"

    def test_the_owning_deployment_can_still_resume_it(self, machine, tmp_path, monkeypatch):
        """The guard must not break the flow it protects."""
        machine("dev")
        api = _aws_api()
        monkeypatch.setattr(aws_cli, "Aws", lambda *args: pytest.fail("a handoff must not require local AWS credentials"))
        directory = tmp_path / "handoff"
        downloaded = aws_cli.run(aws_cli.parser().parse_args(["connect", "--account", "123456789012", "--yes", "--download", str(directory)]), api)

        machine("dev")
        resumed = aws_cli.run(aws_cli.parser().parse_args(["connect", "--resume", str(directory), "--yes"]), api)

        assert resumed["verified"] is True
        assert resumed["connection_id"] == downloaded["connection_id"]


class TestBedrockHandoffCrossesNoDeployment:
    def _download(self, api, directory, monkeypatch):
        monkeypatch.setattr(bedrock_cli, "Aws", lambda *args: pytest.fail("a download must not touch AWS"))
        return bedrock_cli.run(
            bedrock_cli.parser().parse_args(["connect", "--account", "123456789012", "--org", "SOPHOS-IT", "--yes", "--download", str(directory)]),
            api,
        )

    def test_the_downloaded_metadata_carries_the_deployment(self, machine, tmp_path, monkeypatch):
        dev = machine("dev")
        directory = tmp_path / "handoff"

        self._download(_bedrock_api(), directory, monkeypatch)

        metadata = json.loads((directory / "destination.json").read_text())
        assert metadata["deployment_id"] == dev.id
        assert metadata["deployment"] == "dev"

    def test_resuming_it_elsewhere_assigns_no_routing_rule(self, machine, tmp_path, monkeypatch):
        """A wrongly-resumed Bedrock handoff would re-route a whole organization,
        so the refusal must land before the assign call."""
        machine("dev")
        api = _bedrock_api()
        directory = tmp_path / "handoff"
        self._download(api, directory, monkeypatch)

        machine("integration")
        api.calls.clear()
        with pytest.raises(bedrock_cli.CliError) as failure:
            bedrock_cli.run(bedrock_cli.parser().parse_args(["connect", "--resume", str(directory), "--yes"]), api)

        assert failure.value.code == "deployment_mismatch"
        assert [call for call in api.calls if call[0] != "GET"] == [], "a refused resume still changed ADP routing"
