"""CLI helper-script download route — Issues #4146, #4156.

The in-app /setup page offers a Download button per CLI helper file. Before this
route existed the button pointed at `/api/cli/bg-auth.sh`, which nothing served
(and its PowerShell sibling had no source file at all), so both downloads were
dead links.

`ALLOWED_SCRIPTS` below is the serveable set — read it rather than a count here,
which went stale as helpers were added. It holds the `adp` CLI and its
`install.sh` (Issue #4852 — the one-line install path, where install.sh fetches
the rest from this same route), the Cognito auth helper `bg-cognito-auth.sh` that
`adp` wraps, and `bg-gateway-proxy.py` — the localhost auth proxy its `serve`
subcommand starts for Codex. `serve` requires the two to sit side by side, so
serving only the helper left the documented Codex flow unfinishable (Issue #4156).
The domain helpers (`adp-bedrock.py`, `adp-aws.py`, `adp-github*.py`,
`adp-superplane.py`, `adp-flow.py`) provide scriptable destination setup, routing,
personal AWS account connection, provider configuration and delivery-flow control
through the same authenticated APIs as the UI. Only their static code is served
here.

Two deliberate design points:

**Unauthenticated.** The page triggers the download with `window.open`, which
sends no Authorization header — an authenticated route would 401 in the new
tab. Nothing here is secret: the script is public open-source content, carries
no credentials, and is already published in the repo.

**Filename allowlist, not a path join.** `{script_name}` is user input. Joining
it onto a filesystem path would be a path-traversal bug (`../../etc/passwd`).
Instead each permitted name is mapped to a pre-resolved absolute path and
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
# bg-gateway-proxy.py is the Codex-only local auth proxy that `bg-cognito-auth.sh
# serve` starts (Issue #4154). `serve` looks for it as a *sibling* of the helper,
# so a user following cli/README.md §"Using Codex: zero-touch auth with serve"
# needs both files — offering only the helper is a dead end (Issue #4156).
#
# `install.sh` and `adp` are the one-line install path (Issue #4852): install.sh
# is what `curl … | sh` pipes, and it fetches `adp` (plus the two files above)
# from this same route. `adp update` re-pulls install.sh from here too, which is
# what replaces the old manual re-download. Neither file contains a secret, so
# both keep this route's public-content property.
#
# `adp-superplane.py` backs `adp superplane <verb>` (Issue #5039). It must be
# served here for the same reason it is in install.sh's CLI_FILES: `adp`'s
# dispatch resolves it as an installed sibling, so a verb present in `adp`'s
# `case` but absent from this allowlist is a CLI that looks broken for every
# user. It carries no secret — provider values are prompted for at runtime and
# POSTed straight to the vault API, never written into the script.
#
# `adp-flow.py` backs `adp flow <verb>` (Issue #5331), for the same reason: the
# dispatch resolves it as an installed sibling, so a verb in `adp`'s `case` that
# is absent here is a CLI whose new command 404s for every user. It carries no
# secret — it reads the session `adp login` already wrote.

# `adp_deployments.py` owns named deployments and the selection rule (Issue
# #5413). It is not optional: `adp` resolves which deployment a command runs
# against by invoking it at entry, so an install missing this file has no working
# verbs at all. It holds no secret — only names, canonical gateway URLs and
# storage paths.
#
# Deliberately absent:
#   bg-auth.sh   — legacy SigV4 helper, deprecated (cli/README.md)
#   bg-auth.ps1  — never existed in the repo; PowerShell parity is a non-goal
ALLOWED_SCRIPTS: dict[str, Path] = {
    "adp": (_CLI_DIR / "adp").resolve(),
    "adp_common.py": (_CLI_DIR / "adp_common.py").resolve(),
    "adp_deployments.py": (_CLI_DIR / "adp_deployments.py").resolve(),
    "adp-admin.py": (_CLI_DIR / "adp-admin.py").resolve(),
    "install.sh": (_CLI_DIR / "install.sh").resolve(),
    "bg-cognito-auth.sh": (_CLI_DIR / "bg-cognito-auth.sh").resolve(),
    "bg-gateway-proxy.py": (_CLI_DIR / "bg-gateway-proxy.py").resolve(),
    "adp-bedrock.py": (_CLI_DIR / "adp-bedrock.py").resolve(),
    "adp-aws.py": (_CLI_DIR / "adp-aws.py").resolve(),
    "adp-github.py": (_CLI_DIR / "adp-github.py").resolve(),
    "adp-github-admin.py": (_CLI_DIR / "adp-github-admin.py").resolve(),
    "adp-superplane.py": (_CLI_DIR / "adp-superplane.py").resolve(),
    "adp-superplane-onboarding.py": (_CLI_DIR / "adp-superplane-onboarding.py").resolve(),
    "adp-superplane-research.py": (_CLI_DIR / "adp-superplane-research.py").resolve(),
    "adp-models.py": (_CLI_DIR / "adp-models.py").resolve(),
    "adp-flow.py": (_CLI_DIR / "adp-flow.py").resolve(),
    "adp-doctor.py": (_CLI_DIR / "adp-doctor.py").resolve(),
    "command-manifest.json": (_CLI_DIR / "command-manifest.json").resolve(),
    "adp-vault.py": (_CLI_DIR / "adp-vault.py").resolve(),
    "adp-usage.py": (_CLI_DIR / "adp-usage.py").resolve(),
    "adp-agent.py": (_CLI_DIR / "adp-agent.py").resolve(),
    "adp-task.py": (_CLI_DIR / "adp-task.py").resolve(),
    "adp_task_client.py": (_CLI_DIR / "adp_task_client.py").resolve(),
}

SHELL_SCRIPT_MEDIA_TYPE = "text/x-shellscript"
PYTHON_SCRIPT_MEDIA_TYPE = "text/x-python"

# Per-script media type. Keyed off the same allowlisted names, so an entry added
# above without one falls back to the shell type rather than 500-ing.
SCRIPT_MEDIA_TYPES: dict[str, str] = {
    "adp": SHELL_SCRIPT_MEDIA_TYPE,
    "adp_common.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp_deployments.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-admin.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "install.sh": SHELL_SCRIPT_MEDIA_TYPE,
    "bg-cognito-auth.sh": SHELL_SCRIPT_MEDIA_TYPE,
    "bg-gateway-proxy.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-bedrock.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-aws.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-github.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-github-admin.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-superplane.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-superplane-onboarding.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-superplane-research.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-models.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-flow.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-doctor.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "command-manifest.json": "application/json",
    "adp-vault.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-usage.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-agent.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp-task.py": PYTHON_SCRIPT_MEDIA_TYPE,
    "adp_task_client.py": PYTHON_SCRIPT_MEDIA_TYPE,
}


@router.get(
    "/{script_name}",
    summary="Download a CLI helper script",
    description="""
    Serve a CLI helper script as a file attachment (Issues #4146, #4156, #4852).

    Public and unauthenticated by design: the /setup page downloads via
    `window.open`, which cannot attach an Authorization header — and the
    documented `curl … | sh` install line sends no header either. The serveable
    files are `adp`, `install.sh`, `bg-cognito-auth.sh`, `bg-gateway-proxy.py` and
    the `adp-*.py` area helpers, none of which contains secrets.

    `script_name` is matched against an explicit allowlist — it is never
    joined onto a filesystem path — so traversal attempts return 404.
    """,
    responses={
        200: {"description": "Script returned as a file attachment"},
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
        media_type=SCRIPT_MEDIA_TYPES.get(script_name, SHELL_SCRIPT_MEDIA_TYPE),
        filename=script_name,
    )
