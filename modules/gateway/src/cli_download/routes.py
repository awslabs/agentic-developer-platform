"""CLI helper-script download route — Issue #4146.

The in-app /setup page offers a Download button for the Cognito auth helper.
Before this route existed the button pointed at `/api/cli/bg-auth.sh`, which
nothing served (and its PowerShell sibling had no source file at all), so both
downloads were dead links.

Two deliberate design points:

**Unauthenticated.** The page triggers the download with `window.open`, which
sends no Authorization header — an authenticated route would 401 in the new
tab. Nothing here is secret: the script is public open-source content, carries
no credentials, and is already published in the repo.

**Filename allowlist, not a path join.** `{script_name}` is user input. Joining
it onto a filesystem path would be a path-traversal bug (`../../etc/passwd`).
Instead the single permitted name is mapped to a pre-resolved absolute path and
anything else 404s.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cli", tags=["cli"])

# Repo root as seen from this file: src/cli_download/routes.py -> src -> <gateway>.
# Matches the runtime image layout, where WORKDIR /app holds both src/ and cli/
# (see modules/gateway/Dockerfile).
_GATEWAY_ROOT = Path(__file__).resolve().parents[2]
_CLI_DIR = _GATEWAY_ROOT / "cli"

# The allowlist. Keys are the ONLY values of {script_name} that resolve; the
# values are absolute paths, so no user-controlled segment is ever joined.
#
# Deliberately absent:
#   bg-auth.sh   — legacy SigV4 helper, deprecated (cli/README.md)
#   bg-auth.ps1  — never existed in the repo; PowerShell parity is a non-goal
ALLOWED_SCRIPTS: dict[str, Path] = {
    "bg-cognito-auth.sh": (_CLI_DIR / "bg-cognito-auth.sh").resolve(),
}

SHELL_SCRIPT_MEDIA_TYPE = "text/x-shellscript"


@router.get(
    "/{script_name}",
    summary="Download a CLI helper script",
    description="""
    Serve a CLI helper script as a file attachment (Issue #4146).

    Public and unauthenticated by design: the /setup page downloads via
    `window.open`, which cannot attach an Authorization header. The only
    serveable file is `bg-cognito-auth.sh`, which contains no secrets.

    `script_name` is matched against an explicit allowlist — it is never
    joined onto a filesystem path — so traversal attempts return 404.
    """,
    responses={
        200: {"description": "Script returned as a shell-script attachment"},
        404: {"description": "Unknown script name, or the file is missing from the image"},
    },
)
async def download_cli_script(script_name: str) -> FileResponse:
    """Return an allowlisted CLI helper script as a download."""
    script_path = ALLOWED_SCRIPTS.get(script_name)

    if script_path is None:
        # Same 404 for traversal attempts and honest typos — no signal either way.
        logger.info("CLI script download rejected", extra={"script_name": script_name})
        raise HTTPException(
            status_code=404,
            detail={"error": "script_not_found", "message": "Unknown CLI script"},
        )

    if not script_path.is_file():
        # The name is allowlisted but the file is absent — almost always means
        # cli/ was left out of the container image.
        logger.error(
            "Allowlisted CLI script missing from image",
            extra={"script_name": script_name, "expected_path": str(script_path)},
        )
        raise HTTPException(
            status_code=404,
            detail={"error": "script_not_found", "message": "Unknown CLI script"},
        )

    return FileResponse(
        path=script_path,
        media_type=SHELL_SCRIPT_MEDIA_TYPE,
        filename=script_name,
    )
