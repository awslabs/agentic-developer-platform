#!/usr/bin/env python3
"""Provision and then assert the AWS Security Agent client on the runner
(intent #4290, unit U4, issue #4443).

Why one script with two modes instead of two shell steps
--------------------------------------------------------
The nightly's single most likely failure mode is a provisioning step that
lands the package in one interpreter while the assertion runs in another --
the step passes, provisions nothing useful, and every downstream unit then
fails on a runtime the plan believed was fixed.

Guarding against that with a YAML-level convention ("remember to use the same
python") is exactly the kind of assumption the plan asks us not to make. So
both halves live here and both bind to ``sys.executable``:

    provision:  <python> securityagent_preflight.py --provision
    assert:     <python> securityagent_preflight.py

``--provision`` installs into ``sys.executable``; the assertion imports into
the same ``sys.executable``. Interpreter identity is therefore a structural
property of the design rather than a comment nobody re-checks.

Why the install command is read and never retyped
-------------------------------------------------
``.github/security/security-agent-profile.json`` (U0, #4439) is the interface.
It records ``runtime.install_command`` and ``runtime.min_botocore_version``
as *bisected* facts: the spike identified the exact adjacent botocore releases
across which the service model appears, so the floor is that boundary rather
than a guess. Retyping either value here would create a second source of truth
that drifts silently. Both are read at run time, and neither literal appears in
this file or in the workflow -- a rule the quality gate enforces by searching
this source for version literals, so prose here names profile *fields*, never
their values.

Why this fails closed instead of self-healing
---------------------------------------------
The profile's ``runtime.preflight_assertion`` is written as
``python3 -c '...' || pip3 install ...`` -- assertion with a repair fallback.
That shape is right for an operator at a shell and wrong here: it makes the
assertion unable to fail, and the whole point of this step is to be the thing
that fails when provisioning did not take. The repair belongs in
``--provision``, which runs first and unconditionally. This mode only ever
reports.

That distinction is load-bearing for this runner specifically. The U0 spike
executed on the agent-scaledjob pod, whose image already resolves the
service. The nightly runs on ``arc-runner-org``, whose image hard-pins a boto3
release *proven* not to carry ``securityagent`` (see the profile's
``runtime.evidence.baked_pin_lacks_service``). So on the real runner the
provisioning step is emphatically not a no-op, and per the profile's own
``residual_risk`` note this assertion must fail closed if the upgrade does
not take.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess  # nosec B404 - fixed argv from a trusted in-repo artifact, no shell
import sys
from pathlib import Path

PROFILE_PATH = (
    Path(__file__).resolve().parent.parent / "security" / "security-agent-profile.json"
)

SERVICE_NAME = "securityagent"

# Tokens that mean "the pip of whichever interpreter happens to be first on
# PATH". Every one of them is a different-interpreter bug waiting to happen,
# so they are rewritten to `sys.executable -m pip` before execution.
_PIP_TOKENS = ("pip", "pip3")


class PreflightError(RuntimeError):
    """The runner cannot reach the Security Agent service.

    Raised with an operator-actionable message: an unattended nightly that
    fails with a bare traceback gets silently abandoned.
    """


# --------------------------------------------------------------------------
# profile access
# --------------------------------------------------------------------------


def load_profile(path: Path | str = PROFILE_PATH) -> dict:
    """Load the U0 validated profile. Absence is a hard failure: without it
    we have no install command and no version floor, and guessing either
    defeats the point of the artifact."""
    profile_path = Path(path)
    if not profile_path.is_file():
        raise PreflightError(
            f"validated profile artifact not found at {profile_path}. "
            "This file is the source of truth for the install command and the "
            "botocore version floor (see issue #4439); preflight cannot run "
            "without it."
        )
    return json.loads(profile_path.read_text(encoding="utf-8"))


def install_argv(profile: dict, python_executable: str = sys.executable) -> list[str]:
    """Build the provisioning argv from the profile's recorded command.

    The recorded command is authoritative for *what* to install (flags and
    version floor included). This function only rebinds *which* interpreter
    receives it, so the package lands where the assertion will look.
    """
    recorded = (profile.get("runtime", {}).get("install_command") or "").strip()
    if not recorded:
        raise PreflightError(
            "runtime.install_command is empty in the validated profile. U0 "
            "recorded provisioning as 'self_upgradable', which requires an "
            "install command; refusing to invent one."
        )

    tokens = shlex.split(recorded)

    # `pip install ...`               -> <python> -m pip install ...
    # `python3 -m pip install ...`    -> <python> -m pip install ...
    if tokens[0] in _PIP_TOKENS:
        tokens = tokens[1:]
    elif len(tokens) >= 3 and tokens[1] == "-m" and tokens[2] in _PIP_TOKENS:
        tokens = tokens[3:]
    else:
        raise PreflightError(
            f"runtime.install_command does not start with pip or 'python -m pip': "
            f"{recorded!r}. Refusing to guess how to bind it to an interpreter."
        )

    return [python_executable, "-m", "pip", *tokens]


# --------------------------------------------------------------------------
# version comparison
# --------------------------------------------------------------------------


def parse_version(version: str) -> tuple[int, ...]:
    """Parse a dotted numeric version. The profile gate
    (test_security_agent_profile.py) already guarantees the recorded floors
    are dotted numerics, so anything else here came from the installed
    package and is treated as unparseable rather than silently accepted."""
    cleaned = str(version).strip()
    try:
        return tuple(int(part) for part in cleaned.split("."))
    except ValueError as exc:
        raise PreflightError(f"cannot parse version {version!r}") from exc


def version_at_least(observed: str, minimum: str) -> bool:
    """True when `observed` >= `minimum`, compared component-wise.

    Zero-pads the shorter side so a two-component version compares equal to
    its own explicit .0 patch release rather than sorting below it.
    """
    left, right = parse_version(observed), parse_version(minimum)
    width = max(len(left), len(right))
    left += (0,) * (width - len(left))
    right += (0,) * (width - len(right))
    return left >= right


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------


def provision(
    profile: dict | None = None,
    python_executable: str = sys.executable,
    runner=subprocess.run,
) -> list[str]:
    """Install the client into `python_executable`. Returns the argv run.

    Deliberately unconditional. The repo contains ~20 CLI-install steps
    shaped `command -v x || install x`, and on this runner image every one is
    a no-op. That idiom is the reason this unit exists, so it is not reused:
    a step that skips itself when a *stale* version is already present is
    indistinguishable from a step that works, and the boto3 release baked
    into arc-runner-org is exactly such a stale-but-present case.
    """
    profile = load_profile() if profile is None else profile
    argv = install_argv(profile, python_executable)

    print(f"[provision] {shlex.join(argv)}", flush=True)
    completed = runner(argv, check=False)  # nosec B603 - argv from trusted artifact
    if completed.returncode != 0:
        raise PreflightError(
            f"provisioning failed (exit {completed.returncode}): {shlex.join(argv)}. "
            "The Security Agent client is not installable in this interpreter; "
            "the nightly cannot proceed. If this runner image cannot be upgraded "
            "in-session, the runner-image unit (U13) is required."
        )
    return argv


# --------------------------------------------------------------------------
# assertion
# --------------------------------------------------------------------------


def _remediation(profile: dict, python_executable: str) -> str:
    """The actionable half of a failure message."""
    try:
        fix = shlex.join(install_argv(profile, python_executable))
    except PreflightError:
        fix = profile.get("runtime", {}).get("install_command", "<not recorded>")
    return (
        f"Interpreter: {python_executable}\n"
        f"Fix: {fix}\n"
        f"(recorded in {PROFILE_PATH.name} as runtime.install_command)"
    )


def botocore_version() -> str:
    """Read the installed botocore version.

    A separate injectable seam rather than an inline import, so a test that
    supplies a stub session does not also need botocore installed. Without
    this split the "client absent" tests could only run on an interpreter that
    already had botocore -- i.e. they would be unrunnable on exactly the
    clean-interpreter CI machine that most resembles an unprovisioned runner.
    """
    try:
        import botocore  # noqa: PLC0415 - imported late; absence is a checked failure

        return botocore.__version__
    except ImportError as exc:
        raise PreflightError("botocore is not importable") from exc


def assert_client_available(
    profile: dict | None = None,
    session_factory=None,
    python_executable: str = sys.executable,
    version_reader=None,
) -> str:
    """Assert the `securityagent` client is constructible here.

    Returns the resolved botocore version. Raises PreflightError -- never a
    bare ImportError or AttributeError -- so the nightly's failure line names
    a cause and a fix.

    `session_factory` and `version_reader` are injectable so every failure
    path can be tested against stubs instead of requiring a specifically
    (mis)provisioned interpreter.
    """
    profile = load_profile() if profile is None else profile
    runtime = profile.get("runtime", {})
    minimum = runtime.get("min_botocore_version")

    if session_factory is None:
        try:
            import boto3  # noqa: PLC0415 - imported late; absence is a checked failure

            session_factory = boto3.Session
        except ImportError as exc:
            raise PreflightError(
                "boto3 is not importable in this interpreter, so the Security "
                "Agent client cannot be constructed.\n"
                f"{_remediation(profile, python_executable)}"
            ) from exc

    try:
        observed = (version_reader or botocore_version)()
    except PreflightError as exc:
        raise PreflightError(
            f"{exc} in this interpreter.\n{_remediation(profile, python_executable)}"
        ) from exc

    session = session_factory()
    available = session.get_available_services()

    if SERVICE_NAME not in available:
        raise PreflightError(
            f"the {SERVICE_NAME!r} service is not present in this interpreter's "
            f"botocore service model (botocore {observed}, floor {minimum}).\n"
            "Provisioning did not take effect: either the install step did not "
            "run, or it installed into a different interpreter than this one.\n"
            f"{_remediation(profile, python_executable)}"
        )

    if minimum and not version_at_least(observed, minimum):
        # Reachable when a runner image ships a backported service model: the
        # client resolves but sits below the bisected floor, which is not a
        # configuration we have validated.
        raise PreflightError(
            f"botocore {observed} is below the validated floor {minimum}, even "
            f"though {SERVICE_NAME!r} resolves. This combination is unvalidated; "
            "failing closed rather than running the nightly on it.\n"
            f"{_remediation(profile, python_executable)}"
        )

    print(
        f"[preflight] OK: {SERVICE_NAME} available "
        f"(botocore {observed} >= {minimum}, interpreter {python_executable})",
        flush=True,
    )
    return observed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Provision (--provision) or assert (default) the AWS Security Agent "
            "client in THIS interpreter."
        )
    )
    parser.add_argument(
        "--provision",
        action="store_true",
        help=(
            "Install the client using the profile's recorded command, bound to "
            "this interpreter. Runs unconditionally."
        ),
    )
    parser.add_argument(
        "--profile",
        default=str(PROFILE_PATH),
        help="Path to the validated profile artifact.",
    )
    args = parser.parse_args(argv)

    try:
        profile = load_profile(args.profile)
        if args.provision:
            provision(profile)
        else:
            assert_client_available(profile)
    except PreflightError as exc:
        print(f"::error title=Security Agent preflight::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
