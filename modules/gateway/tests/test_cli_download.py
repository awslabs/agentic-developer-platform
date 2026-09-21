"""
Unit tests for GET /cli/{script_name} (Issues #4146, #4156, #4852).

The in-app /setup page's Download buttons hit this route via `window.open`, and
since #4852 the documented one-line install pipes `install.sh` from it into `sh`.
So it must:
- serve all four CLI files as attachments with no Authorization header (neither
  `window.open` nor `curl … | sh` sends one),
- serve the real on-disk files, not stale vendored copies — the page tells users
  these are the files that `import`, `apiKeyHelper` and `serve` run,
- reject anything not on the allowlist with a 404, including path traversal.
  `{script_name}` is user input; a filesystem join here would be a traversal bug.

The files are required together. `bg-cognito-auth.sh serve` looks for
`bg-gateway-proxy.py` as its sibling, so a Codex user who can download only the
helper cannot complete the documented flow (Issue #4156). Since #4852,
`install.sh` fetches `adp` plus both of those from this same route, so a gap in
the allowlist breaks the install line rather than just one button.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.cli_download.routes import ALLOWED_SCRIPTS, router

test_app = FastAPI()
test_app.include_router(router)
client = TestClient(test_app)

ADP_SCRIPT = "adp"
INSTALL_SCRIPT = "install.sh"
HELPER_SCRIPT = "bg-cognito-auth.sh"
PROXY_SCRIPT = "bg-gateway-proxy.py"

# Resolved independently of the route module so a wrong path in ALLOWED_SCRIPTS
# cannot make these tests pass. tests/ -> <gateway> -> cli/
CLI_DIR = Path(__file__).resolve().parents[1] / "cli"

# (script_name, expected media type) — the proxy is Python, the rest are shell,
# and serving one as the other is what the media-type assertion guards.
SERVEABLE = [
    (ADP_SCRIPT, "text/x-shellscript"),
    (INSTALL_SCRIPT, "text/x-shellscript"),
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

    def test_allowlist_contains_exactly_the_expected_cli_files(self):
        """Legacy bg-auth.sh (deprecated) and bg-auth.ps1 (no source file) stay
        out. Pinned as a set so adding a file to cli/ never makes it publicly
        downloadable by accident — this route is unauthenticated."""
        assert set(ALLOWED_SCRIPTS) == {
            ADP_SCRIPT,
            INSTALL_SCRIPT,
            HELPER_SCRIPT,
            PROXY_SCRIPT,
            "adp_common.py",
            # Issue #5413: `adp` resolves the selected deployment through this at
            # entry, so an install without it has no working verbs. Names, URLs
            # and paths only — no secret to expose on an unauthenticated route.
            "adp_deployments.py",
            "adp-admin.py",
            "adp-bedrock.py",
            "adp-aws.py",
            # Issue #5184: added in the same PR that ships the helper, so the
            # command is never advertised before its file is downloadable.
            "adp-github.py",
            "adp-github-admin.py",
            # Issue #5039: `adp superplane`. Carries no secret — it reads the
            # session the existing `adp login` already wrote, which is the whole
            # point of the story, so publishing it exposes nothing.
            "adp-superplane.py",
            "adp-models.py",
            # Issue #5331: `adp flow`. Same property — it reads the session
            # `adp login` already wrote and adds no credential store of its own,
            # so serving it publicly exposes nothing. Added in the PR that ships
            # the helper, never before it.
            "adp-flow.py",
        }

    def test_install_script_is_fetchable_the_way_curl_pipes_it(self):
        """The documented install line is `curl … | sh`, so this must come back
        as a runnable POSIX script body and not, say, an HTML error page."""
        resp = client.get(f"/cli/{INSTALL_SCRIPT}")

        assert resp.status_code == 200
        assert resp.text.startswith("#!/bin/sh")
        # The flag the install line passes — if this is missing, every documented
        # invocation fails on an unknown option.
        assert "--gateway-url" in resp.text

    def test_installer_can_fetch_every_file_it_asks_for(self):
        """install.sh downloads these three by name from this route; a mismatch
        between the two lists is a broken install, not a broken button."""
        installer = (CLI_DIR / INSTALL_SCRIPT).read_text()

        for name in (ADP_SCRIPT, HELPER_SCRIPT, PROXY_SCRIPT):
            assert name in installer, f"install.sh no longer installs {name}"
            assert client.get(f"/cli/{name}").status_code == 200

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
            "claude-settings.example.json",
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
