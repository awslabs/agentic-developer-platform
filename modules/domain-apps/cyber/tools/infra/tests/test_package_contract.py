"""Packaging and deployment ownership checks that do not invoke AWS."""
from pathlib import Path
import subprocess

INFRA = Path(__file__).resolve().parents[1]


def test_container_copies_only_owned_tool_packages():
    lines = (INFRA.parent / "Dockerfile").read_text().splitlines()
    copies = [line.split()[1] for line in lines if line.startswith("COPY ")]
    assert copies == ["modules/tools/adp_tools", "modules/domain-apps/cyber/tools/cyber_tools", "modules/domain-apps/cyber/agent/skills/url-analysis"]
    assert 'CMD ["cyber_tools.handler.lambda_handler"]' in lines


def test_browser_service_image_has_only_owned_sources_and_no_listener():
    dockerfile = (INFRA.parent / "Dockerfile.browser").read_text()
    assert "COPY modules/tools/adp_tools/ /app/adp_tools/" in dockerfile
    assert "COPY modules/domain-apps/cyber/tools/cyber_tools/ /app/cyber_tools/" in dockerfile
    assert "COPY modules/domain-apps/cyber/agent/skills/url-analysis/ /app/skills/url-analysis/" in dockerfile
    assert 'CMD ["python3", "-m", "cyber_tools.browser_http"]' in dockerfile
    assert "EXPOSE " not in dockerfile
    ignored = (INFRA.parent / "Dockerfile.browser.dockerignore").read_text()
    assert "**/.env*" in ignored and "!modules/tools/adp_tools/**" in ignored


def test_shared_api_stage_and_public_lambda_are_not_owned():
    source = "\n".join(path.read_text() for path in INFRA.glob("*.tf"))
    for resource in ("aws_api_gateway_stage", "aws_api_gateway_deployment", "aws_lambda_function_url"):
        assert f'resource "{resource}"' not in source
    assert 'authorization = "AWS_IAM"' in source
    assert 'source_account' in source
    assert 'WEBHOOK_EVENTS_TABLE' not in source
    assert 'CYBER_TOOLS_TABLE' in source


def test_publish_and_apply_scripts_are_valid_and_never_publish_shared_stage():
    for script in ("build-image.sh", "deploy.sh"):
        path = INFRA / script
        subprocess.run(["bash", "-n", str(path)], check=True)
        text = path.read_text()
        assert "create-deployment" not in text and "update-stage" not in text
    assert 'get-caller-identity' in (INFRA / "deploy.sh").read_text()


def test_lambda_image_builder_disables_multi_manifest_provenance():
    assert "--platform linux/amd64 --provenance=false" in (INFRA / "build-image.sh").read_text()
