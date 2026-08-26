"""
Unit tests for GET /cli/{script_name} (Issues #4146, #4156).

The in-app /setup page's Download buttons hit this route via `window.open`, so it
must:
- serve `bg-cognito-auth.sh` and `bg-gateway-proxy.py` as file attachments with no
  Authorization header (window.open cannot send one),
- serve the real on-disk files, not stale vendored copies — the page tells users
  these are the files that `import`, `apiKeyHelper` and `serve` run,
- reject anything not on the allowlist with a 404, including path traversal.
  `{script_name}` is user input; a filesystem join here would be a traversal bug.

Both files are required together: `bg-cognito-auth.sh serve` looks for
`bg-gateway-proxy.py` as its sibling, so a Codex user who can download only the
helper cannot complete the documented flow (Issue #4156).
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.cli_download.routes import ALLOWED_SCRIPTS, router

test_app = FastAPI()
test_app.include_router(router)
client = TestClient(test_app)

HELPER_SCRIPT = "bg-cognito-auth.sh"
PROXY_SCRIPT = "bg-gateway-proxy.py"

# Resolved independently of the route module so a wrong path in ALLOWED_SCRIPTS
# cannot make these tests pass. tests/ -> <gateway> -> cli/
CLI_DIR = Path(__file__).resolve().parents[1] / "cli"

# (script_name, expected media type) — the proxy is Python, the helper is shell,
# and serving one as the other is what the media-type assertion guards.
SERVEABLE = [
    (HELPER_SCRIPT, "text/x-shellscript"),
    (PROXY_SCRIPT, "text/x-python"),
]
SCRIPT_NAMES = [name for name, _ in SERVEABLE]


@pytest.mark.unit
class TestCliScriptDownload:
    @pytest.mark.parametrize("script_name", SCRIPT_NAMES)
    def test_serves_the_script(self, script_name):
        resp = client.get(f"/cli/{script_name}")

        assert resp.status_code == 200
        assert resp.text.startswith("#!")

    @pytest.mark.parametrize(("script_name", "media_type"), SERVEABLE)
    def test_served_as_attachment_with_its_own_media_type(self, script_name, media_type):
        """Content type + filename drive the browser's save-as, not a render."""
        resp = client.get(f"/cli/{script_name}")

        assert resp.headers["content-type"].startswith(media_type)
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment")
        assert script_name in disposition

    @pytest.mark.parametrize("script_name", SCRIPT_NAMES)
    def test_no_authorization_header_required(self, script_name):
        """window.open sends no Authorization header — auth would 401 the download."""
        resp = client.get(f"/cli/{script_name}", headers={})

        assert resp.status_code == 200

    @pytest.mark.parametrize("script_name", SCRIPT_NAMES)
    def test_served_bytes_equal_the_on_disk_script(self, script_name):
        """Guards against serving a stale vendored copy of either file."""
        resp = client.get(f"/cli/{script_name}")

        assert resp.content == (CLI_DIR / script_name).read_bytes()

    def test_allowlist_contains_exactly_the_helper_and_the_proxy(self):
        """Legacy bg-auth.sh (deprecated) and bg-auth.ps1 (no source file) stay out."""
        assert set(ALLOWED_SCRIPTS) == {HELPER_SCRIPT, PROXY_SCRIPT}

    @pytest.mark.parametrize(
        "script_name",
        [
            "../../etc/passwd",
            "..%2f..%2fetc%2fpasswd",
            "....//....//etc/passwd",
            "../src/app.py",
            "../cli/bg-auth.sh",
            "bg-auth.sh",  # deprecated legacy helper — deliberately not serveable
            "bg-auth.ps1",  # never existed in the repo
            "install.sh",
            "README.md",
        ],
    )
    def test_rejects_anything_off_the_allowlist(self, script_name):
        resp = client.get(f"/cli/{script_name}")

        assert resp.status_code == 404
        # No file contents leaked, and no 500 from an unguarded path join.
        assert "root:" not in resp.text
        assert "def create_app" not in resp.text

    def test_traversal_with_absolute_path_is_rejected(self):
        resp = client.get("/cli//etc/passwd")

        assert resp.status_code == 404
        assert "root:" not in resp.text

    @pytest.mark.parametrize("script_name", SCRIPT_NAMES)
    def test_allowlisted_path_points_at_a_real_file(self, script_name):
        """A missing file here means cli/ was left out of the container image."""
        assert ALLOWED_SCRIPTS[script_name].is_file()
