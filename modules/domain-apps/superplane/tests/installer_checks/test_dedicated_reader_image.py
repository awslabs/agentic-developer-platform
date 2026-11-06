"""The selected API must contain reader code before either rollout path starts."""

from types import SimpleNamespace

import pytest

from installation import reader_image, runner
from installation.config import Refusal, image

from .test_selected_api_base import ImageTools, Probe, result


@pytest.mark.parametrize("mode", ["local", "cluster"])
@pytest.mark.parametrize("contract", ["supported", "missing_module", "old_image"])
def test_reader_capability_gates_both_image_paths(
    tmp_path, environment, release, monkeypatch, mode, contract
):
    environment.pop("execution")
    environment.pop("controller_profiles")
    environment["image_execution"] = "cluster" if mode == "cluster" else "docker"
    checked = []

    def reader_result():
        if contract == "missing_module":
            return SimpleNamespace(returncode=1, stdout="", stderr="missing module")
        return result(reader_image.REPORT if contract == "supported" else {})

    class ReaderTools(ImageTools):
        def call(self, args, **kwargs):
            if reader_image.VERIFY_PROGRAM in args:
                checked.append((args, kwargs))
                assert args == [
                    "docker",
                    "run",
                    "--rm",
                    "--network=none",
                    "--entrypoint",
                    "python",
                    image(release, "superplane-api"),
                    "-c",
                    reader_image.VERIFY_PROGRAM,
                ]
                assert kwargs == {"allow_failure": True}
                return reader_result()
            return super().call(args, **kwargs)

    class ReaderProbe(Probe):
        def run(self, component, command, **kwargs):
            if reader_image.VERIFY_PROGRAM in command:
                checked.append((command, kwargs))
                assert component == "superplane-api"
                assert command == ["python", "-c", reader_image.VERIFY_PROGRAM]
                assert kwargs == {}
                return reader_result()
            return super().run(component, command, **kwargs)

    tools = ReaderTools(release, None, None, environment)
    monkeypatch.setattr(runner.httpx, "get", tools.get)
    monkeypatch.setattr(runner, "ClusterProbe", ReaderProbe)
    installer = runner.Installer(
        environment, release, tmp_path, tools, control_plane_only=True
    )
    # Input validation is covered separately. Here only the optional feature's
    # image gate is selected, with no live credentials or reader configuration.
    installer.env["dedicated_readers"] = {"enabled_for_image_check": True}
    if contract == "supported":
        installer.images()
        assert (
            installer.receipt["dedicated_reader_image_contract"] == reader_image.REPORT
        )
    else:
        with pytest.raises(Refusal, match="dedicated reader"):
            installer.images()
        assert "dedicated_reader_image_contract" not in installer.receipt
    assert len(checked) == 1
