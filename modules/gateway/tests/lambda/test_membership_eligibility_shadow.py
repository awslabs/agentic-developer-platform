"""Issue #4849: the membership-eligibility read is SHADOW ONLY in both auth Lambdas.

The issue's fifth deliverable is "no sign-in behavior change" — the read may be
exercised and logged, but must not move a single allow/deny outcome. T5 (#4844)
is what makes it authoritative.

That is a claim about *behavior*, so these tests drive the real handlers with the
reader returning each of its three verdicts and assert the outcome is identical
to what it was without the read. They also pin the harder half of the guarantee:
the read raising must not deny anyone. The broker is the sole enforcement point
for GitHub sign-in (#3986) and the pre-signup trigger denies by *raising*, so an
unhandled exception in a code path that is not supposed to have an opinion yet
would be a total login outage — the #3999 code-before-config shape.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# Puts modules/gateway/lambda/shared on sys.path, where membership_eligibility
# lives — the same import surface both Lambdas get once packaged (deploy-broker.sh
# and pre_signup.tf each copy that file in flat beside the handler).
from ._handler_loader import load_handler

_GATEWAY_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(mod_name: str, path: Path, *extra_syspath: Path) -> ModuleType:
    """Load a Lambda module from an explicit path under a unique module name.

    Not ``_handler_loader.load_handler``: the broker's handler imports its sibling
    modules flat (``from allowlist import …``), which only resolves with its own
    directory on ``sys.path`` — that is the layout the zip has at runtime. Only the
    broker needs this; pre-signup uses the shared loader (#4848).

    A failed exec is evicted from ``sys.modules`` so a broken import surfaces on
    every test rather than leaving a half-initialised module cached for later ones.
    """
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    for entry in extra_syspath:
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[mod_name]
        raise
    return module


@pytest.fixture
def broker() -> ModuleType:
    broker_dir = _GATEWAY_ROOT / "lambda" / "github-auth-broker"
    return _load("shadow_broker_handler", broker_dir / "handler.py", broker_dir)


@pytest.fixture
def pre_signup() -> ModuleType:
    """Load the pre-signup trigger that is actually DEPLOYED.

    Since #4848 there is only one copy: ``infra/modules/cognito/pre_signup.tf``
    packages ``lambda/pre-signup/handler.py`` (entered in the zip as
    ``pre_signup.py`` to match the ``pre_signup.handler`` handler string). The
    former ``infra/modules/cognito/lambda/pre_signup.py`` duplicate is deleted, so
    "the deployed copy" and "the tested copy" are now the same file and this
    fixture can use the shared handler loader like every other lambda suite.
    """
    return load_handler("pre-signup")


# Every verdict the reader can return, plus a raise. None of them may change an
# outcome while the read is in shadow mode.
ALL_READER_BEHAVIOURS = [
    ("eligible", None),
    ("not_eligible", None),
    ("unavailable", None),
    (None, RuntimeError("ddb down")),
]


def _patch_reader(verdict, exc):
    """Patch the shared reader as the Lambdas import it (``from ... import``)."""
    fake = MagicMock()
    if exc is not None:
        fake.check_platform_membership.side_effect = exc
    else:
        fake.check_platform_membership.return_value = verdict
    return patch.dict(sys.modules, {"membership_eligibility": fake}), fake


# ---------------------------------------------------------------------------
# github-auth-broker
# ---------------------------------------------------------------------------


def _callback_event(state: str = "s" * 32) -> dict:
    return {
        "queryStringParameters": {"code": "gh-code", "state": state},
        "headers": {"Cookie": f"gh_oauth_state={state}"},
    }


def _drive_broker_callback(broker, *, denial):
    """Run _handle_callback with everything but the allowlist decision stubbed."""
    with (
        patch.object(broker, "_verify_state", return_value=True),
        patch.object(broker, "_get_github_client_secret", return_value="secret"),
        patch.object(broker, "_get_github_client_id", return_value="client-id"),
        patch.object(broker, "exchange_code_for_token", return_value="gh-token"),
        patch.object(
            broker,
            "get_github_user",
            return_value={
                "id": 20402445,
                "login": "octocat",
                "email": "octocat@example.com",
                "name": "Octo Cat",
                "avatar_url": "https://example.invalid/a.png",
            },
        ),
        patch.object(broker, "_check_allowlist", return_value=denial),
        patch.object(broker, "provision_and_authenticate", return_value={"id_token": "t"}),
        patch.object(broker, "_emit_session_handoff", return_value={"statusCode": 302, "allowed": True}),
    ):
        return broker._handle_callback(_callback_event())


@pytest.mark.parametrize("verdict,exc", ALL_READER_BEHAVIOURS)
def test_broker_allowed_login_still_allowed(broker, verdict, exc):
    """An allowlisted user signs in regardless of what the projection says.

    ``not_eligible`` is the case that matters: a user with no projected
    membership (every user, before reconciliation runs) must still get in.
    """
    ctx, fake = _patch_reader(verdict, exc)
    with ctx:
        result = _drive_broker_callback(broker, denial=None)
    assert result.get("allowed") is True
    # The read did run — shadow mode means inert, not skipped.
    fake.check_platform_membership.assert_called_once()


@pytest.mark.parametrize("verdict,exc", ALL_READER_BEHAVIOURS)
def test_broker_denied_login_still_denied(broker, verdict, exc):
    """``eligible`` must not let a non-allowlisted user in either.

    The read is inert in BOTH directions; a projection hit is not an override.
    """
    ctx, _ = _patch_reader(verdict, exc)
    with ctx:
        result = _drive_broker_callback(broker, denial="not_authorized")
    assert result["statusCode"] == 302
    assert "not_authorized" in result["headers"]["Location"]


def test_broker_reads_the_numeric_github_id_not_the_login(broker):
    """The projection is keyed on the immutable numeric id, never the login."""
    ctx, fake = _patch_reader("eligible", None)
    with ctx:
        _drive_broker_callback(broker, denial=None)
    (arg,), _ = fake.check_platform_membership.call_args
    assert arg == "20402445"


def test_broker_shadow_helper_never_raises(broker):
    """Direct guard on the wrapper, independent of the callback plumbing."""
    ctx, _ = _patch_reader(None, RuntimeError("boom"))
    with ctx:
        broker._log_membership_eligibility_shadow(20402445, "octocat", None)


def test_broker_shadow_helper_survives_a_missing_module(broker):
    """Code can ship before the packaging change lands on a given deploy path."""
    with patch.dict(sys.modules, {"membership_eligibility": None}):
        broker._log_membership_eligibility_shadow(20402445, "octocat", None)


# ---------------------------------------------------------------------------
# pre-signup Cognito trigger
# ---------------------------------------------------------------------------


def _signup_event(username: str = "GitHub_20402445") -> dict:
    return {
        "triggerSource": "PreSignUp_ExternalProvider",
        "userName": username,
        "request": {"userAttributes": {"preferred_username": "octocat"}},
        "response": {},
    }


@pytest.mark.parametrize("verdict,exc", ALL_READER_BEHAVIOURS)
def test_pre_signup_open_mode_still_confirms(pre_signup, verdict, exc):
    """Issue #4844: 'open' now also needs ALLOW_OPEN_SIGNUP, as the broker always has.

    The shadow-mode guarantee this test exists for is unchanged and still asserted:
    whatever the projection says, an open-mode sign-up is confirmed. Only the
    precondition moved — the mode alone was a divergence from the broker (#3986),
    so the flag is now set here the way Terraform sets it in every environment.
    """
    ctx, fake = _patch_reader(verdict, exc)
    with (
        ctx,
        patch.object(pre_signup, "ALLOWLIST_MODE", "open"),
        patch.object(pre_signup, "ALLOW_OPEN_SIGNUP", True),
    ):
        result = pre_signup.handler(_signup_event(), None)
    assert result["response"]["autoConfirmUser"] is True
    fake.check_platform_membership.assert_called_once()


@pytest.mark.parametrize("verdict,exc", ALL_READER_BEHAVIOURS)
def test_pre_signup_open_mode_without_the_flag_denies_regardless_of_the_read(pre_signup, verdict, exc):
    """The other half of #4844's alignment, still shadow-safe.

    An unflagged 'open' denies for the *misconfiguration*, never because of what
    the projection said — the denial must be identical across all four reader
    behaviours, which is what parametrising this proves.
    """
    ctx, _ = _patch_reader(verdict, exc)
    with (
        ctx,
        patch.object(pre_signup, "ALLOWLIST_MODE", "open"),
        patch.object(pre_signup, "ALLOW_OPEN_SIGNUP", False),
        pytest.raises(Exception, match="misconfiguration"),
    ):
        pre_signup.handler(_signup_event(), None)


@pytest.mark.parametrize("verdict,exc", ALL_READER_BEHAVIOURS)
def test_pre_signup_org_mode_allows_org_member(pre_signup, verdict, exc):
    """``not_eligible`` must not deny a legitimate org member."""
    ctx, _ = _patch_reader(verdict, exc)
    with (
        ctx,
        patch.object(pre_signup, "ALLOWLIST_MODE", "org"),
        patch.object(pre_signup, "_check_org_membership", return_value=True),
    ):
        result = pre_signup.handler(_signup_event(), None)
    assert result["response"]["autoConfirmUser"] is True


@pytest.mark.parametrize("verdict,exc", ALL_READER_BEHAVIOURS)
def test_pre_signup_org_mode_denies_non_member(pre_signup, verdict, exc):
    """``eligible`` must not smuggle a non-org-member past the org check."""
    ctx, _ = _patch_reader(verdict, exc)
    with (
        ctx,
        patch.object(pre_signup, "ALLOWLIST_MODE", "org"),
        patch.object(pre_signup, "_check_org_membership", return_value=False),
        pytest.raises(Exception, match="not a member of an allowed organization"),
    ):
        pre_signup.handler(_signup_event(), None)


def test_pre_signup_admin_create_user_still_passes_through(pre_signup):
    """Non-external triggers return before the shadow read — unchanged (#3986)."""
    ctx, fake = _patch_reader("not_eligible", None)
    event = _signup_event()
    event["triggerSource"] = "PreSignUp_AdminCreateUser"
    with ctx:
        result = pre_signup.handler(event, None)
    assert result is event
    fake.check_platform_membership.assert_not_called()


def test_pre_signup_extracts_numeric_id_from_cognito_username(pre_signup):
    assert pre_signup._extract_github_user_id("GitHub_20402445") == "20402445"


@pytest.mark.parametrize(
    "username",
    ["GitHub_octocat", "octocat", "", "GitHub_", "Google_1234abcd"],
)
def test_pre_signup_refuses_to_guess_a_non_numeric_id(pre_signup, username):
    """No login fallback: a login is not a valid projection key.

    ``_extract_github_username`` deliberately falls back to the email prefix;
    doing that here would look up a *different* user's projection and return a
    confidently wrong answer instead of no answer.
    """
    assert pre_signup._extract_github_user_id(username) == ""


def test_pre_signup_skips_the_read_when_no_numeric_id(pre_signup):
    """A login-shaped userName yields no id, so the shadow read is skipped, not guessed.

    Issue #4844 added the ALLOW_OPEN_SIGNUP precondition to 'open' mode; the
    assertion about the *read* is unchanged.
    """
    ctx, fake = _patch_reader("eligible", None)
    with (
        ctx,
        patch.object(pre_signup, "ALLOWLIST_MODE", "open"),
        patch.object(pre_signup, "ALLOW_OPEN_SIGNUP", True),
    ):
        result = pre_signup.handler(_signup_event(username="GitHub_octocat"), None)
    assert result["response"]["autoConfirmUser"] is True
    fake.check_platform_membership.assert_not_called()


def test_pre_signup_shadow_helper_never_raises(pre_signup):
    ctx, _ = _patch_reader(None, RuntimeError("boom"))
    with ctx:
        pre_signup._log_membership_eligibility_shadow("GitHub_20402445", "octocat")


def test_pre_signup_shadow_helper_survives_a_missing_module(pre_signup):
    with patch.dict(sys.modules, {"membership_eligibility": None}):
        pre_signup._log_membership_eligibility_shadow("GitHub_20402445", "octocat")
