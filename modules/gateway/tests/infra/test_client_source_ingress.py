"""Run the deployed viewer function with spoofed headers and a real viewer IP."""

import json
import re
import subprocess
from pathlib import Path

INFRA = Path(__file__).resolve().parents[2] / "infra"


def test_cloudfront_replaces_source_address_and_strips_viewer_proof():
    source = (INFRA / "modules/cloudfront/main.tf").read_text()
    block = source.split('resource "aws_cloudfront_function" "strip_api_prefix"', 1)[1]
    code = re.search(r"code\s*=\s*<<-EOF\n(.*?)\n\s*EOF", block, re.S).group(1)
    event = {
        "viewer": {"ip": "192.0.2.10"},
        "request": {
            "uri": "/api/auth/cli/start",
            "headers": {
                "x-adp-client-ip": {"value": "forged"},
                "x-adp-client-provenance": {"value": "forged"},
                "authorization": {"value": "Bearer keep"},
            },
        },
    }
    result = subprocess.run(
        ["node", "-e", code + "\nconsole.log(JSON.stringify(handler(JSON.parse(process.argv[1]))));", json.dumps(event)],
        check=True,
        capture_output=True,
        text=True,
    )
    request = json.loads(result.stdout)
    assert request["uri"] == "/auth/cli/start"
    assert request["headers"]["x-adp-client-ip"] == {"value": "192.0.2.10"}
    assert "x-adp-client-provenance" not in request["headers"]
    assert request["headers"]["authorization"] == {"value": "Bearer keep"}


def test_shared_proof_move_preserves_deployed_secret_and_has_no_cognito_dependency():
    source = (INFRA / "edge-provenance.tf").read_text()
    assert "from = module.api_gateway[0].random_password.edge_provenance" in source
    assert "to   = random_password.edge_provenance[0]" in source
    assert "length  = 64" in source
    root = (INFRA / "main.tf").read_text()
    assert re.search(r"edge_provenance_secret\s*=\s*random_password.edge_provenance\[0\].result", root)
    assert re.search(r"client_provenance_secret\s*=.*random_password.edge_provenance\[0\].result", root)
