#!/usr/bin/env python3
"""E01/E02/E03 on the disposable instance: install the served release, log in.

This is step 1 and step 2 of the executable path. It runs the documented user
install line — `curl -fsSL <gateway>/cli/install.sh | sh` — because that, not a
file copy, is what E01 claims works. `install.sh` copies from its own directory
when it is run from a checkout and only downloads when it arrives on stdin, so
piping is the difference between testing the published release and testing the
repo we already have.

E01 asserts the download is unauthenticated, the install is immediately
executable, and every installed helper matches a hash the orchestrator derived
independently from the release manifest. Comparing the install against hashes
observed from the same download would only prove the file did not change between
two reads of it.

E02 asserts a real Cognito login through the CLI's own challenge flow. A seeded
token would satisfy `adp status` and prove nothing, so the fixture supplies a
username and password, the CLI performs `POST /auth/cli/password`, and the
evidence must show a challenge was completed. The negatives matter as much: wrong
credentials must be refused, and a non-admin identity must be denied the admin
session.

E03 asserts `adp admin setup` reports accurate per-provider states, and that a
rerun after an interruption completes only what is missing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import common
from common import require


def _install(config, evidence, home, env):
    """E01: the served release, installed the way a user installs it."""
    evidence["stage"] = "install"
    gateway = config["gateway_url"].rstrip("/")
    prefix = home / ".adp" / "bin"

    # The unauthenticated download, checked separately from the install so a 403
    # or an SPA HTML fallback is distinguishable from a bad install.
    code, out, _err = common.bounded(
        [
            "curl",
            "-fsS",
            "-o",
            str(home / "install.sh"),
            "-w",
            "%{http_code}",
            gateway + "/cli/install.sh",
        ],
        env=env,
        timeout=120,
    )
    require(code == 0, "The install script could not be downloaded without credentials")
    download_status = int((out or "0").strip() or 0)
    expected = config.get("expected_hashes") or {}
    require(expected.get("install.sh"), "No expected installer hash was supplied")
    require(
        hashlib.sha256((home / "install.sh").read_bytes()).hexdigest()
        == expected["install.sh"],
        "Downloaded installer differs from the expected release",
    )

    # Pipe it into sh, exactly as documented, so the installer takes its
    # download path rather than copying from a local checkout.
    argv = [
        "sh",
        "-s",
        "--",
        "--gateway-url",
        gateway,
        "--prefix",
        str(prefix),
        "--no-path-edit",
    ]
    evidence["transcript"].append(
        "curl -fsSL <gateway>/cli/install.sh | " + common.sanitize(argv)
    )
    code, _out, _err = common.bounded(
        argv, env=env, timeout=300, stdin=(home / "install.sh").read_text()
    )
    require(
        code == 0,
        f"The published install.sh exited {code}; the release did not install",
    )

    installed = common.hashes_of(prefix)
    binary = prefix / "adp"
    require(binary.is_file(), "install.sh did not place `adp` in the install prefix")
    executable = os.access(binary, os.X_OK)

    # R10: the expected hashes are the orchestrator's, derived from the release
    # manifest at the revision under test — never re-read from this download.
    # The installer is verified before execution; it does not install itself.
    expected = {
        name: digest for name, digest in expected.items() if name != "install.sh"
    }
    require(
        expected,
        "No expected release hashes were supplied; a mixed or stale install could not be detected",
    )
    missing = sorted(set(expected) - set(installed))
    mismatched = sorted(
        name
        for name, value in expected.items()
        if name in installed and installed[name] != value
    )
    evidence["install"] = {
        "success": code == 0,
        "download_status": download_status or 200,
        "executable": executable,
        "helpers": sorted(installed),
        "expected_helper_count": len(expected),
        "missing_helpers": missing,
        "mismatched_helpers": mismatched,
        "hashes_match": not missing and not mismatched,
    }
    require(
        not missing,
        "The install is incomplete: the release manifest lists helpers that were not installed",
    )
    require(
        not mismatched,
        "Installed helper bytes differ from the release manifest; refusing to evaluate a mixed install",
    )

    version_code, _payload = common.Cli(binary, env, evidence["transcript"]).run(
        ["version"], expected=0, json_output=False
    )
    evidence["install"]["version_command_ok"] = version_code == 0
    return prefix


def _login(config, evidence, cli, env, home, *, challenges_required=True):
    """E02: the real challenge flow, plus the negatives that must fail."""
    evidence["stage"] = "login"
    login = {"challenges": [], "seeded_token_used": False}

    # Wrong credentials first, while there is no session to confuse the result.
    bad = json.dumps(
        {
            "username": common.fixture_secret(config, env, "admin_username"),
            "password": "wrong-" + config["evaluation_id"],
        }
    )
    code, payload = cli.run(
        ["admin", "login", "--credentials-stdin"], expected=None, stdin_text=bad
    )
    login["bad_credentials_rejected"] = (
        code != 0 and not (home / ".bedrock-gateway" / "tokens.json").exists()
    )
    require(
        login["bad_credentials_rejected"],
        "Wrong administrator credentials did not fail the login",
    )

    # The real login. The fixture carries a temporary password, so the first
    # response is NEW_PASSWORD_REQUIRED and the CLI must complete that challenge
    # — which is the whole point of E02.
    original_password = common.fixture_secret(config, env, "admin_password")
    credentials = {
        "username": common.fixture_secret(config, env, "admin_username"),
        "password": original_password,
    }
    rotated = (
        common.fixture_secret(config, env, "admin_new_password", default="")
        if challenges_required
        else ""
    )
    # The challenge the fixture is built to provoke. The CLI's envelope does not
    # name the challenges it answered, so this is what we EXPECT; the assertion
    # below is what proves it actually happened.
    challenges = ["NEW_PASSWORD_REQUIRED"] if rotated else []
    if rotated:
        credentials["new_password"] = rotated
    payload = cli.json(
        ["admin", "login", "--credentials-stdin"], stdin_text=json.dumps(credentials)
    )
    require(
        payload.get("status") == "verified",
        f"adp admin login reported {payload.get('status')!r} instead of verified",
    )
    login["authenticated"] = True
    session = home / ".bedrock-gateway" / "tokens.json"
    require(session.exists(), "A verified login did not persist a session")

    # The credential that works from here on. After a completed rotation the
    # fixture's original password is retired, so every later re-login in this
    # journey must use the new one or it authenticates as nobody.
    effective = json.dumps(
        {
            "username": credentials["username"],
            "password": rotated or original_password,
        }
    )

    # Challenge evidence. This has to come from the login exchange itself, not
    # from an after-the-fact query: `GET /auth/cli/admin-session` returns
    # `{verified, user_id, org_id, role}` and no challenge history at all, so the
    # earlier `completed_challenges` read was always an empty list and
    # `challenge_completed` collapsed to `bool(rotated)` — the CLI's own word for
    # whether we had asked it to rotate.
    #
    # What proves a challenge really happened is that the OLD password no longer
    # authenticates and the new one does. Cognito only retires the temporary
    # password on a completed NEW_PASSWORD_REQUIRED, so this is the server's
    # answer and it cannot be produced by a seeded token.
    login["challenges"] = sorted(challenges)
    if rotated:
        stale, _payload = cli.run(
            ["admin", "login", "--credentials-stdin"],
            expected=None,
            stdin_text=json.dumps(
                {"username": credentials["username"], "password": original_password}
            ),
        )
        login["prior_password_retired"] = stale != 0
        require(
            login["prior_password_retired"],
            "The password supplied before the challenge still authenticates; no "
            "NEW_PASSWORD_REQUIRED challenge was actually completed",
        )
        # That attempt replaced the session on disk with nothing usable; the rest
        # of the run needs the post-challenge one back.
        cli.json(["admin", "login", "--credentials-stdin"], stdin_text=effective)
    login["challenge_completed"] = bool(login["challenges"]) and (
        not rotated or login.get("prior_password_retired", False)
    )
    require(
        not challenges_required or login["challenge_completed"],
        "The login completed without any challenge; a seeded or pre-rotated identity cannot satisfy E02",
    )

    # The identity the gateway itself attributes this session to. `user_id` is
    # what the usage log keys on, so E08 needs it from here rather than guessing
    # that it equals the username.
    _status, admin_session = common.api(
        config, "/auth/cli/admin-session", _access_token(session), expect=(200,)
    )
    require(
        (admin_session or {}).get("verified"),
        "The gateway did not confirm an admin session for the identity that logged in",
    )
    login["user_id"] = admin_session.get("user_id") or ""
    login["org_id"] = admin_session.get("org_id") or ""
    login["role"] = admin_session.get("role") or ""
    # The identity later journeys scope their assertions to. A username is not a
    # credential — the password never leaves `fixture_secret` — and E06 needs it to
    # name the user a routing rule is checked against.
    login["username"] = credentials["username"]

    # A non-admin identity must be denied the admin session.
    non_admin = (
        common.fixture_secret(config, env, "non_admin_username", default="")
        if challenges_required
        else ""
    )
    if non_admin:
        code, _payload = cli.run(
            ["admin", "login", "--credentials-stdin"],
            expected=None,
            stdin_text=json.dumps(
                {
                    "username": non_admin,
                    "password": common.fixture_secret(
                        config, env, "non_admin_password"
                    ),
                }
            ),
        )
        login["non_admin_denied"] = code != 0
        require(
            login["non_admin_denied"],
            "A non-admin identity was granted an admin session",
        )
        # Restore the admin session the rest of the run depends on.
        cli.json(["admin", "login", "--credentials-stdin"], stdin_text=effective)
    else:
        login["non_admin_denied"] = False
        login["non_admin_note"] = "no non-admin fixture identity was configured"

    # Refresh must renew without another interactive login.
    before = _access_token(session)
    code, _payload = cli.run(["refresh"], expected=0, json_output=False)
    login["refresh_succeeded"] = code == 0 and _access_token(session) != ""
    require(login["refresh_succeeded"], "adp refresh did not renew the session")
    login["refresh_rotated_token"] = _access_token(session) != before

    evidence["login"] = login
    return _access_token(session)


def _onboard(config, evidence, token):
    """Give a run-created login the ADP account that later cases require.

    A Cognito identity is not an ADP account. E02 needs its own identities — the
    shared fixture is CONFIRMED, so it issues no challenge, and it is an admin, so
    it cannot be the negative — but the identity created for E02 is then inherited
    by every LATER case in the run, and those touch user-scoped records. The
    gateway maps a Cognito subject to a `users` row and answers 404
    `user_not_found` when there is none, which is how E13's
    `adp aws connect --download` came to report `failed`/exit 5 where it expects
    `pending`/exit 4: a failure whose cause was installed two stages earlier.
    Reproduced live against dev — the same command returns `pending` for the
    shared fixture, which HAS a row, and `user_not_found` for a run-created
    identity, which does not.

    Done HERE, on the instance, rather than beside the Cognito create in the
    orchestrator's `_admin_fixtures`, for one reason: this route requires a
    platform-admin BEARER token, and the only such token that exists without
    minting a second one is the session the CLI just earned by completing E02's
    NEW_PASSWORD_REQUIRED challenge. Having the orchestrator log in to get its own
    would consume that challenge before the CLI could be tested against it, which
    is E02 itself. So the identity registers its own ADP account with the session
    it just proved.

    Through the product's own supported onboarding route, never a database write:
    the run must exercise the path an operator would, or the fixture proves a
    shape the product never actually produces. `cognito_identity` adopts the login
    that already exists, with `expected_sub` as the immutable proof that route
    demands — a mutable email is deliberately not sufficient there, and it should
    not be here either.

    The created row is reported as a resource so the sweep removes it. Deleting it
    cascades to the Cognito login, which is why `cleanup.ORDER` putting
    `cognito_user` first matters: by the time this row is deleted the login is
    already gone, and the Cognito deleter treats absence as the success it is.

    Returns without asserting when the run did not create the identity — a shared
    fixture already has its account and must not be re-provisioned.
    """
    username = config.get("created_username") or ""
    if not username:
        return
    evidence["stage"] = "onboard"
    org = (evidence.get("login") or {}).get("org_id") or ""
    subject = (evidence.get("login") or {}).get("user_id") or ""
    require(org, "The gateway attributed no organization to the run's own identity")
    require(subject, "The gateway attributed no subject to the run's own identity")

    # The org's own team, read rather than constructed. `create_user` refuses a
    # team that does not exist in the org, and the default it would derive
    # (`<org>-team-default`) is not what every org actually has — dev's
    # `adp-platform` does not. Verified live: the constructed name answers 404
    # "Team does not exist in the requested organization".
    base = "/api/admin/identity/organizations/" + org
    _status, listing = common.api(config, base + "/users", token, expect=(200,))
    team = next(
        (
            row.get("team_id")
            for row in ((listing or {}).get("users") or [])
            if row.get("team_id")
        ),
        "",
    )
    require(
        team,
        f"Organization {org} has no user carrying a team, so no existing team could "
        "be resolved to register this run's identity into",
    )

    status, created = common.api(
        config,
        base + "/users",
        token,
        method="POST",
        body={
            "email": username,
            "name": "ADP CLI uplift evaluation " + config["evaluation_id"],
            "role": "platform_admin",
            "team_id": team,
            # No mail for an undeliverable address, matching MessageAction=SUPPRESS
            # on the Cognito create.
            "send_invite": False,
            "cognito_identity": {"username": username, "expected_sub": subject},
        },
        expect=(201,),
    )
    identifier = (created or {}).get("id") or ""
    require(
        identifier,
        f"Registering the run's identity returned HTTP {status} with no user id",
    )
    require(
        (created or {}).get("cognito_sub") == subject,
        "The registered ADP account was not bound to the login that created it",
    )
    # Reported so the orchestrator records it BEFORE anything else uses it, and so
    # an interrupted run still leaves it findable. Non-secret: an org id and a row
    # id, both already in the evidence.
    evidence.setdefault("resources", []).append(["adp_user", org + "/" + identifier])
    evidence["onboard"] = {
        "org_id": org,
        "team_id": team,
        "user_id": identifier,
        "cognito_sub_bound": True,
    }


def _access_token(path):
    try:
        return json.loads(Path(path).read_text()).get("access_token") or ""
    except (OSError, ValueError):
        return ""


def _session_document(config, evidence, prefix, home, work_dir):
    """The session every later journey reuses, and the CLI it reuses it with.

    E06, E08 and E14 all read this out of the run document: without it they get
    empty tokens and an empty `cli_path`, and a routing or inference failure would
    really be "install_auth published nothing" — the ambiguity this harness exists
    to remove. So it is assembled here, from the tokens the product itself wrote,
    and asserted non-empty before any of them can be launched.

    HOME here is a per-run temporary directory that is deleted when this journey
    ends, so the CLI is COPIED to a durable location on the instance. A later
    journey materializes its own HOME from these tokens and needs the binary to
    still exist.
    """
    tokens = json.loads((home / ".bedrock-gateway" / "tokens.json").read_text())
    require(tokens.get("access_token"), "The persisted session carries no access token")

    durable = Path(work_dir) / "cli"
    if durable.resolve() != prefix.resolve():
        if durable.exists():
            shutil.rmtree(durable)
        durable.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(prefix, durable)
    binary = durable / "adp"
    require(
        binary.is_file() and os.access(binary, os.X_OK),
        f"The installed CLI was not preserved at {binary} for later journeys",
    )

    login = evidence.get("login") or {}
    # The tokens stay on the instance. Only a non-secret reference is exported,
    # because `common.emit()` redacts every credential-shaped value on its way out
    # — which previously turned this session into the literal string "<redacted>"
    # and left every later journey authenticating with a truthy placeholder.
    # See `common.save_session()` for why the vault is on-instance and not in S3.
    evidence["session"] = common.save_session(
        {
            "cli_path": str(binary),
            "cli_config": json.loads(
                (home / ".bedrock-gateway" / "config.json").read_text()
            ),
            "access_token": tokens["access_token"],
            "id_token": tokens.get("id_token", ""),
            "refresh_token": tokens.get("refresh_token", ""),
            "expires_at": tokens.get("expires_at", 0),
            "username": login.get("username", ""),
            "user_id": login.get("user_id", ""),
            "org_id": login.get("org_id", ""),
            # Only set when the run CREATED the identity, because that is what
            # makes it this run's to delete. A fixture identity must survive
            # cleanup.
            "created_username": config.get("created_username", ""),
        },
        work_dir=work_dir,
    )


def _setup(config, evidence, cli):
    """E03: accurate provider states, and a rerun that completes only the gaps."""
    evidence["stage"] = "setup"
    # Also `expected=None`: `adp admin setup --dry-run` maps a `pending` exit 4
    # down to 0 deliberately, but it does NOT remap the `failed` exit 5. So a
    # single failed provider would abort this journey on the exit code before
    # E03 could report the accurate-state finding that IS its subject. The
    # envelope is read either way.
    dry = cli.json(["admin", "setup", "--dry-run"], expected=None)
    states = {
        step.get("name"): step.get("status")
        for step in ((dry.get("detail") or {}).get("steps") or [])
    }
    require(states, "adp admin setup --dry-run reported no provider steps")
    allowed = {"configured", "verified", "pending", "failed", "unavailable"}
    accurate = all(status in allowed for status in states.values())

    # A dry run must not have changed anything: the second dry run must agree.
    again = cli.json(["admin", "setup", "--dry-run"], expected=None)
    repeat = {
        step.get("name"): step.get("status")
        for step in ((again.get("detail") or {}).get("steps") or [])
    }
    require(
        repeat == states, "A dry-run setup changed the reported configuration state"
    )

    # The rerun-after-interruption property: running setup again must not
    # duplicate a provider entry or regress a configured provider to pending.
    #
    # `expected=None`, not 0. `adp` exits 4 when any provider is `pending` and 5
    # when one is `failed` — the documented envelope convention this class's own
    # docstring describes ("the JSON envelope is the contract, not the exit
    # code"). Demanding 0 here conflated "the command ran" with "every provider
    # is fully configured", so E03 failed on an environment where bedrock and
    # github are legitimately unprovisioned — reporting a harness expectation as
    # a product defect. What E03 actually asserts is the shape of the rerun (no
    # duplicated step, no regression), which is checked below and is independent
    # of how much happens to be configured.
    rerun = cli.json(["admin", "setup", "--yes"], expected=None)
    require(
        rerun.get("status") in ("configured", "verified", "pending"),
        f"adp admin setup --yes reported {rerun.get('status')!r}; a rerun must not fail",
    )
    steps = (rerun.get("detail") or {}).get("steps") or []
    names = [step.get("name") for step in steps]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    regressed = sorted(
        name
        for name, status in states.items()
        if status in ("configured", "verified")
        and next((s.get("status") for s in steps if s.get("name") == name), None)
        == "pending"
    )
    evidence["setup"] = {
        "reported_states": states,
        "statuses_accurate": accurate,
        "dry_run_is_read_only": repeat == states,
        "rerun_completed_missing_only": not regressed,
        "duplicates": duplicates,
        "regressed": regressed,
    }
    require(not duplicates, "A setup rerun duplicated a provider step")
    require(not regressed, "A setup rerun regressed an already-configured provider")


def execute(config, evidence):
    """Install, log in, and check setup — in one session on one instance."""
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="adp-install-auth-") as temporary:
        home = Path(temporary)
        # HOME is the isolation boundary: `adp` derives ~/.bedrock-gateway from
        # it, so a per-run HOME keeps this session out of any other identity's
        # config directory without needing a CLI change.
        env = common.clean_env(
            config,
            HOME=str(home),
            AWS_CONFIG_FILE=str(home / "aws-config"),
            AWS_SHARED_CREDENTIALS_FILE=str(home / "no-credentials"),
        )
        prefix = _install(config, evidence, home, env)
        if not config.get("login_required", True):
            evidence.update(stage="complete", success=True)
            return
        cli = common.Cli(prefix / "adp", env, evidence["transcript"])
        token = _login(
            config,
            evidence,
            cli,
            env,
            home,
            challenges_required=config.get("admin_challenges_required", True),
        )
        # Before `_setup` and before any later journey: everything downstream of
        # here authenticates as this identity, and a user-scoped route refuses a
        # login with no ADP account. A no-op for the shared fixture.
        _onboard(config, evidence, token)
        if config.get("admin_setup_required", True):
            _setup(config, evidence, cli)
        evidence["session_token_present"] = bool(token)
        # The run-owned durable directory the ec2 stage created, which is where
        # both the preserved CLI and the session vault belong. `work_dir`, NOT
        # `work_dir/cli`: `_session_document` appends "cli" itself, and the vault
        # path a later journey is told to read is derived from this same value —
        # so passing the CLI subdirectory here would put the vault somewhere no
        # journey looks for it. Falls back to the journey's own temp directory only
        # when no work_dir was supplied, which is a degraded single-journey run.
        _session_document(
            config, evidence, prefix, home, config.get("work_dir") or temporary
        )
        evidence.update(stage="complete", success=True)


if __name__ == "__main__":
    import sys

    sys.exit(common.run_script(execute))
