import importlib.util
from pathlib import Path

PLACEHOLDER = Path(__file__).resolve().parents[1] / "bootstrap_placeholder.py"


def test_bootstrap_placeholder_denies_without_reflecting_request():
    spec = importlib.util.spec_from_file_location("bootstrap_placeholder", PLACEHOLDER)
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    for event in ({}, {"headers": {"authorization": "example"}, "body": {"id": "private"}}):
        result = handler.handler(event, None)
        denial = result["mcp"]["transformedGatewayResponse"]
        assert denial["statusCode"] == 403
        assert denial["body"]["error"]["message"] == "Interceptor not released"
        assert denial["body"]["id"] is None
        assert "private" not in repr(result)


def test_placeholder_and_real_release_share_the_lambda_handler_path(tmp_path):
    import os
    import subprocess
    import sys
    from zipfile import ZipFile

    from tools_interceptor.handler import handler as real_handler

    assert callable(real_handler)
    archive = tmp_path / "placeholder.zip"
    with ZipFile(archive, "w") as package:
        package.writestr("tools_interceptor/__init__.py", "")
        package.writestr("tools_interceptor/handler.py", PLACEHOLDER.read_text())
    result = subprocess.run(
        [sys.executable, "-c", "from tools_interceptor.handler import handler; print(handler({}, None)['mcp']['transformedGatewayResponse']['statusCode'])"],
        cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": str(archive)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "403"
