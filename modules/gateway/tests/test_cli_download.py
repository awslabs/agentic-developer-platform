"""
Unit tests for GET /cli/{script_name} (Issue #4146).

The in-app /setup page's Download button hits this route via `window.open`, so it
must:
- serve `bg-cognito-auth.sh` as a shell-script attachment with no Authorization
  header (window.open cannot send one),
- serve the real on-disk file, not a stale vendored copy — the page tells users
  this is the helper that `import` and `apiKeyHelper` run,
- reject anything not on the allowlist with a 404, including path traversal.
  `{script_name}` is user input; a filesystem join here would be a traversal bug.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.cli_download.routes import ALLOWED_SCRIPTS, router

test_app = FastAPI()
test_app.include_router(router)
client = TestClient(test_app)

SCRIPT_NAME = "bg-cognito-auth.sh"
ROUTE = f"/cli/{SCRIPT_NAME}"

# Resolved independently of the route module so a wrong path in ALLOWED_SCRIPTS
# cannot make these tests pass. tests/ -> <gateway> -> cli/
ON_DISK_SCRIPT = Path(__file__).resolve().parents[1] / "cli" / SCRIPT_NAME


@pytest.mark.unit
class TestCliScriptDownload:
    def test_serves_the_cognito_helper(self):
        resp = client.get(ROUTE)

        assert resp.status_code == 200
        assert resp.text.startswith("#!")

    def test_served_as_shell_script_attachment(self):
        """Content type + filename drive the browser's save-as, not a render."""
        resp = client.get(ROUTE)

        assert resp.headers["content-type"].startswith("text/x-shellscript")
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment")
        assert SCRIPT_NAME in disposition

    def test_no_authorization_header_required(self):
        """window.open sends no Authorization header — auth would 401 the download."""
        resp = client.get(ROUTE, headers={})

        assert resp.status_code == 200

    def test_served_bytes_equal_the_on_disk_script(self):
        """Guards against serving a stale vendored copy of the helper."""
        resp = client.get(ROUTE)

        assert resp.content == ON_DISK_SCRIPT.read_bytes()

    def test_allowlist_contains_only_the_cognito_helper(self):
        """Legacy bg-auth.sh (deprecated) and bg-auth.ps1 (no source file) stay out."""
        assert set(ALLOWED_SCRIPTS) == {SCRIPT_NAME}

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

    def test_allowlisted_path_points_at_a_real_file(self):
        """A missing file here means cli/ was left out of the container image."""
        assert ALLOWED_SCRIPTS[SCRIPT_NAME].is_file()
