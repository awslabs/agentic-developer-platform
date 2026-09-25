"""Issue #4844: the allowlist-mode rule, across every live copy of it.

``ALLOWLIST_MODE`` is dispatched in two places, and their drift is what #4848 and
#4849 were filed over:

===  ==========================================================  =============
 #   file                                                        live gate?
===  ==========================================================  =============
 1   ``lambda/github-auth-broker/handler.py::_check_allowlist``   ✅ the only one
 2   ``lambda/pre-signup/handler.py::handler``                    ❌ never fires
===  ==========================================================  =============

A third file, ``lambda/github-auth-broker/allowlist.py::check_org_membership``, is
sometimes counted as a third copy. It is not a mode dispatcher: it is ``org``
mode's GitHub-API helper and never reads ``ALLOWLIST_MODE``. It needs no change
for a new mode, so it is not in this matrix.

**Copy #2 is not the live gate**, and this file does not pretend otherwise.
``admin_create_user`` (what the broker calls to provision) does not fire
``PreSignUp_ExternalProvider``, and the trigger passes ``PreSignUp_AdminCreateUser``
straight through, so the broker is the sole enforcement point for GitHub sign-in
(#3986; ``lambda/github-auth-broker/handler.py``). A green matrix here therefore
proves **nothing** about live sign-in — it is a drift guard, so that a future
change to the trigger cannot resurrect a rule the broker abandoned. The
behavioural gate test that does matter drives the broker end-to-end and lives in
``lambda/github-auth-broker/tests/test_handler.py``.

The one deliberate, documented disagreement is ``explicit``: copy #2 has a working
DynamoDB allowlist, copy #1 denies the mode as unimplemented. Aligning that by
deleting a working implementation would be a regression, so it is asserted as a
known divergence rather than silently "fixed" — and asserting it means a future
change to either side has to come here and say so.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from ._handler_loader import load_handler

_GATEWAY_ROOT = Path(__file__).resolve().parent.parent.parent

# Verdicts of the shared reader (lambda/shared/membership_eligibility.py).
_ELIGIBLE = "eligible"
_NOT_ELIGIBLE = "not_eligible"
_UNAVAILABLE = "unavailable"


def _org_check_verdicts() -> tuple[str, str, str]:
    """The three results of ``check_org_membership`` — read from the real module.

    #5666 (A11): imported rather than hardcoded so renaming a verdict in
    ``lambda/github-auth-broker/allowlist.py`` fails these tests instead of
    silently making them assert against a string the code no longer returns.
    """
    spec = importlib.util.spec_from_file_location(
        "parity_allowlist_verdicts",
        _GATEWAY_ROOT / "lambda" / "github-auth-broker" / "allowlist.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ALLOWED, module.DENIED, module.UNVERIFIED


_ALLOWED, _DENIED, _UNVERIFIED = _org_check_verdicts()


def _load_broker() -> ModuleType:
    """Load the broker handler with its own dir on sys.path.

    The broker imports its siblings flat (``from allowlist import …``), which is
    the layout its zip has at runtime and only resolves with its directory on the
    path. Loaded under a name unique to this suite so it cannot collide with the
    other suites that load the same file.
    """
    mod_name = "parity_broker_handler"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    broker_dir = _GATEWAY_ROOT / "lambda" / "github-auth-broker"
    if str(broker_dir) not in sys.path:
        sys.path.insert(0, str(broker_dir))
    spec = importlib.util.spec_from_file_location(mod_name, broker_dir / "handler.py")
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
    return _load_broker()


@pytest.fixture
def pre_signup() -> ModuleType:
    """The deployed pre-signup copy — since #4848 the only one."""
    return load_handler("pre-signup")


def _fake_reader(verdict=None, exc=None):
    """Stand in for the shared reader, which both copies import lazily."""
    fake = MagicMock()
    fake.ELIGIBLE = _ELIGIBLE
    fake.NOT_ELIGIBLE = _NOT_ELIGIBLE
    fake.UNAVAILABLE = _UNAVAILABLE
    if exc is not None:
        fake.check_platform_membership.side_effect = exc
    else:
        fake.check_platform_membership.return_value = verdict
    return fake


# ---------------------------------------------------------------------------
# Uniform "does this copy allow?" adapters
#
# The two copies signal denial differently by design: the broker returns an error
# code for a redirect, the trigger raises (that is how a Cognito trigger denies).
# Each adapter collapses its copy to a bool so the matrix can compare rules
# rather than idioms.
# ---------------------------------------------------------------------------

_GITHUB_ID = "20402445"
_LOGIN = "octocat"


def _broker_allows(broker, mode, *, reader=None, org_member=True, allow_open=False, org_result=None) -> bool:
    """#5666 (A11): ``org_result`` overrides the bool to reach UNVERIFIED.

    ``org_member`` cannot express the third state ``check_org_membership`` actually
    returns, which is why that branch had no coverage.
    """
    broker.ALLOWLIST_MODE = mode
    broker.ALLOWED_ORGS = "my-org"
    broker.ALLOW_OPEN_SIGNUP = allow_open
    org_result = org_result if org_result is not None else (_ALLOWED if org_member else _DENIED)
    with (
        patch.object(broker, "check_org_membership", return_value=org_result),
        patch.object(broker, "_get_github_org_token", return_value="org-token"),
        patch.dict(sys.modules, {"membership_eligibility": reader or _fake_reader(_NOT_ELIGIBLE)}),
    ):
        return broker._check_allowlist(_LOGIN, "user-token", _GITHUB_ID) is None


def _pre_signup_allows(pre_signup, mode, *, reader=None, org_member=True, allow_open=False, on_allowlist=False, org_result=None) -> bool:
    """``org_result`` is adapted, not ignored — #5666 (A11).

    This copy's ``_check_org_membership`` returns a bool and already collapses its
    own failures to ``False`` (``lambda/pre-signup/handler.py``), so an
    unverifiable check is expressed here as ``False``. The rule being compared
    across copies is "an indeterminate org check does not grant", and both copies
    must satisfy it even though they encode the third state differently.
    """
    if org_result is not None:
        org_member = org_result == _ALLOWED
    pre_signup.ALLOWLIST_MODE = mode
    pre_signup.ALLOWED_ORGS = "my-org"
    pre_signup.ALLOW_OPEN_SIGNUP = allow_open
    event = {
        "triggerSource": "PreSignUp_ExternalProvider",
        "userName": f"GitHub_{_GITHUB_ID}",
        "request": {"userAttributes": {"preferred_username": _LOGIN, "email": f"{_LOGIN}@example.com"}},
        "response": {},
    }
    with (
        patch.object(pre_signup, "_check_org_membership", return_value=org_member),
        patch.object(pre_signup, "_check_explicit_allowlist", return_value=on_allowlist),
        patch.dict(sys.modules, {"membership_eligibility": reader or _fake_reader(_NOT_ELIGIBLE)}),
    ):
        try:
            result = pre_signup.handler(event, None)
        except Exception:
            return False
        return result["response"].get("autoConfirmUser") is True


# Both adapters, so every case below runs against every copy.
COPIES = [
    pytest.param("broker", id="broker"),
    pytest.param("pre_signup", id="pre-signup"),
]


def _allows(copy, broker, pre_signup, mode, **kw) -> bool:
    return _broker_allows(broker, mode, **kw) if copy == "broker" else _pre_signup_allows(pre_signup, mode, **kw)


# ---------------------------------------------------------------------------
# The matrix: every mode × every copy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize(
    "mode",
    ["", "orgg", "ORG_", "platfrom", "plat form", "None", "true", "1", "deny"],
)
def test_unknown_mode_denies_in_every_copy(copy, broker, pre_signup, mode):
    """#3986's fail-closed default: anything that is not a known granting mode denies.

    Includes near-misses of the new mode name (``platfrom``) — a typo in an
    operator's tfvars must lock the door, not open it.
    """
    assert _allows(copy, broker, pre_signup, mode) is False


@pytest.mark.parametrize("copy", COPIES)
def test_org_mode_allows_member_in_every_copy(copy, broker, pre_signup):
    assert _allows(copy, broker, pre_signup, "org", org_member=True) is True


@pytest.mark.parametrize("copy", COPIES)
def test_org_mode_denies_non_member_in_every_copy(copy, broker, pre_signup):
    assert _allows(copy, broker, pre_signup, "org", org_member=False) is False


@pytest.mark.parametrize("copy", COPIES)
def test_open_mode_denies_without_the_flag_in_every_copy(copy, broker, pre_signup):
    """Issue #4844 aligned this: pre-signup used to auto-confirm 'open' unflagged.

    That was a real divergence — the broker has required the acknowledgement since
    #3986 while this copy allowed everyone. Both now deny.
    """
    assert _allows(copy, broker, pre_signup, "open", allow_open=False) is False


@pytest.mark.parametrize("copy", COPIES)
def test_open_mode_allows_with_the_flag_in_every_copy(copy, broker, pre_signup):
    assert _allows(copy, broker, pre_signup, "open", allow_open=True) is True


@pytest.mark.parametrize("copy", COPIES)
def test_platform_mode_allows_member_in_every_copy(copy, broker, pre_signup):
    """The new mode: a platform membership is sufficient, with no GitHub org tie.

    ``org_member=False`` is the point — this user belongs to no allowed GitHub
    org, which is exactly the admin-created-org case the mode exists for.
    """
    reader = _fake_reader(_ELIGIBLE)
    assert _allows(copy, broker, pre_signup, "platform", reader=reader, org_member=False) is True


@pytest.mark.parametrize("copy", COPIES)
def test_platform_mode_denies_non_member_in_every_copy(copy, broker, pre_signup):
    """No membership denies even for a GitHub org member — GitHub is not the authority."""
    reader = _fake_reader(_NOT_ELIGIBLE)
    assert _allows(copy, broker, pre_signup, "platform", reader=reader, org_member=True) is False


@pytest.mark.parametrize("copy", COPIES)
def test_platform_mode_denies_when_source_unavailable_in_every_copy(copy, broker, pre_signup):
    """Fail-closed: "could not check" must not fall through to a grant."""
    reader = _fake_reader(_UNAVAILABLE)
    assert _allows(copy, broker, pre_signup, "platform", reader=reader, org_member=True) is False


@pytest.mark.parametrize("copy", COPIES)
def test_platform_mode_denies_when_read_raises_in_every_copy(copy, broker, pre_signup):
    """A raising read denies.

    The #4849 shadow wrapper deliberately swallows exceptions — correct while the
    verdict was inert. Once it is authoritative, swallowing is fail-OPEN, so the
    enforcing path must not reuse that behaviour.
    """
    reader = _fake_reader(exc=RuntimeError("ddb down"))
    assert _allows(copy, broker, pre_signup, "platform", reader=reader, org_member=True) is False


@pytest.mark.parametrize("copy", COPIES)
def test_platform_mode_denies_when_reader_is_missing_from_the_zip(copy, broker, pre_signup):
    """ImportError denies too.

    The reader is a shared file each Lambda's packaging copies in flat
    (``infra/modules/cognito/pre_signup.tf``, ``scripts/deploy-broker.sh``). If a
    packaging change drops it, the mode cannot be enforced — so it must not look
    enforced. Simulated by making the import itself fail.
    """
    with patch.dict(sys.modules, {"membership_eligibility": None}):
        # A None entry in sys.modules makes `import membership_eligibility` raise
        # ImportError, which is what a missing file does at runtime.
        assert _allows(copy, broker, pre_signup, "platform") is False


@pytest.mark.parametrize("copy", COPIES)
def test_platform_mode_denies_an_unrecognised_verdict_in_every_copy(copy, broker, pre_signup):
    """A verdict neither copy knows is treated as "not proven", i.e. denied.

    Guards a future third verdict being added to the reader and silently reading
    as a grant in one copy.
    """
    reader = _fake_reader("some_new_verdict")
    assert _allows(copy, broker, pre_signup, "platform", reader=reader) is False


@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize("mode", ["PLATFORM", " platform ", "Platform", " ORG", "OPEN "])
def test_mode_parsing_is_case_and_space_insensitive_in_every_copy(copy, broker, pre_signup, mode):
    """Both copies must normalise identically, or a stray space is a divergence.

    Each mode here is spelled to be ALLOWED once normalised, so a copy that fails
    to normalise fails the assert rather than passing by accidentally denying.
    """
    reader = _fake_reader(_ELIGIBLE)
    assert _allows(copy, broker, pre_signup, mode, reader=reader, allow_open=True) is True


# ---------------------------------------------------------------------------
# org mode: the "could not verify" result — #5666 (A11)
#
# ``check_org_membership`` has THREE results (allowlist.py): ALLOWED, DENIED, and
# UNVERIFIED for "the check itself did not complete" — a missing/unapproved org
# token, a GitHub 5xx, a network fault. The matrix above only ever exercised the
# first two, so the branch that decides what an *indeterminate* org check does had
# no coverage in either copy. That is the branch most worth pinning: it is the one
# an attacker can influence by making the check fail, and it is exactly the
# "unknown/unavailable allowlist result" this contract area names.
#
# It is also already correct in the broker (#3986 returns org_check_unavailable,
# which denies) — so these are evidence tests, not repairs. Without them a future
# "simplification" of the three-state result into a bool would read UNVERIFIED as
# truthy and fail open with nothing to stop it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("copy", COPIES)
def test_org_mode_denies_when_the_membership_check_cannot_complete(copy, broker, pre_signup):
    """An indeterminate org check must deny in every copy."""
    assert _allows(copy, broker, pre_signup, "org", org_result=_UNVERIFIED) is False


def test_org_mode_reports_unverified_distinctly_from_not_authorized(broker):
    """An unverifiable check and a proven non-membership must stay distinguishable.

    Collapsing them is the ambiguity #3986 was filed to fix: a missing org token or
    an unapproved OAuth App would present as a legitimate denial, so an operator
    debugging "why can nobody sign in" is told the users are simply not members.
    Asserted on the broker because it is the live gate and the only copy that
    returns an error code at all.
    """
    broker.ALLOWLIST_MODE = "org"
    broker.ALLOWED_ORGS = "my-org"
    with (
        patch.object(broker, "check_org_membership", return_value=_UNVERIFIED),
        patch.object(broker, "_get_github_org_token", return_value="org-token"),
    ):
        unverified = broker._check_allowlist(_LOGIN, "user-token", _GITHUB_ID)
    with (
        patch.object(broker, "check_org_membership", return_value=_DENIED),
        patch.object(broker, "_get_github_org_token", return_value="org-token"),
    ):
        denied = broker._check_allowlist(_LOGIN, "user-token", _GITHUB_ID)

    assert unverified == "org_check_unavailable"
    assert denied == "not_authorized"
    assert unverified != denied, "an unverifiable check must not be reported as a proven denial"


def test_org_mode_denies_when_no_org_token_is_configured_and_github_rejects(broker):
    """The documented fallback path still ends in a denial, not a grant.

    With no ``GITHUB_TOKEN_SECRET_ARN`` the broker falls back to the user's own
    OAuth token, which only works if the OAuth App is org-approved. When it is not,
    GitHub's answer is unverifiable — and that must deny.
    """
    broker.ALLOWLIST_MODE = "org"
    broker.ALLOWED_ORGS = "my-org"
    with (
        patch.object(broker, "check_org_membership", return_value=_UNVERIFIED),
        patch.object(broker, "_get_github_org_token", return_value=None),
    ):
        assert broker._check_allowlist(_LOGIN, "user-token", _GITHUB_ID) == "org_check_unavailable"


def test_org_mode_with_no_allowed_orgs_configured_denies(broker):
    """An empty ALLOWED_ORGS list must not mean "everyone".

    ``org`` mode with nothing configured is a misconfiguration, and the fail-closed
    reading is that a user provably belongs to none of zero orgs.
    """
    broker.ALLOWLIST_MODE = "org"
    broker.ALLOWED_ORGS = ""
    with patch.object(broker, "_get_github_org_token", return_value="org-token"):
        # The real helper decides; no membership can be proven against an empty list.
        assert broker._check_allowlist(_LOGIN, "user-token", _GITHUB_ID) is not None


# ---------------------------------------------------------------------------
# Known, deliberate divergence
# ---------------------------------------------------------------------------


def test_explicit_mode_divergence_is_known_and_documented(broker, pre_signup):
    """``explicit`` is the one mode the copies disagree on, on purpose (#4844).

    The broker denies it as unimplemented (#3986); pre-signup has a working DDB
    allowlist. #4844 chose to document rather than "align" this, because aligning
    downward means deleting a working implementation. If you change either side,
    change this test deliberately — that is the point of asserting it.
    """
    assert _broker_allows(broker, "explicit") is False
    assert _pre_signup_allows(pre_signup, "explicit", on_allowlist=True) is True
    # And the shared half of the rule still holds: not on the list ⇒ denied.
    assert _pre_signup_allows(pre_signup, "explicit", on_allowlist=False) is False


# ---------------------------------------------------------------------------
# The identity key the new mode looks up
# ---------------------------------------------------------------------------


def test_platform_mode_looks_up_the_github_id_not_the_login(broker):
    """The projection is id-keyed because GitHub logins are renameable.

    Passing a login would look up the wrong user (or nobody) rather than fail, so
    this pins the argument. Asserted on the broker because it is the live gate.
    """
    reader = _fake_reader(_ELIGIBLE)
    broker.ALLOWLIST_MODE = "platform"
    with patch.dict(sys.modules, {"membership_eligibility": reader}):
        broker._check_allowlist(_LOGIN, "user-token", _GITHUB_ID)
    reader.check_platform_membership.assert_called_once_with(_GITHUB_ID)


def test_pre_signup_platform_mode_denies_a_username_with_no_numeric_id(pre_signup):
    """No id in the Cognito userName ⇒ deny, never guess.

    ``_extract_github_user_id`` deliberately does not fall back to the login the
    way ``_extract_github_username`` does. With nothing to look up there is no
    membership to prove, so the answer is denial.
    """
    reader = _fake_reader(_ELIGIBLE)
    pre_signup.ALLOWLIST_MODE = "platform"
    event = {
        "triggerSource": "PreSignUp_ExternalProvider",
        "userName": "not-a-github-username",
        "request": {"userAttributes": {"preferred_username": _LOGIN}},
        "response": {},
    }
    with patch.dict(sys.modules, {"membership_eligibility": reader}):
        with pytest.raises(Exception):
            pre_signup.handler(event, None)
    reader.check_platform_membership.assert_not_called()


def test_pre_signup_admin_create_user_still_passes_through_under_platform_mode(pre_signup):
    """The mode must not change what this trigger does NOT gate.

    ``PreSignUp_AdminCreateUser`` is how the broker's provisioning arrives here,
    and it is passed through by design — the broker has already decided. A new
    mode that started gating it would deny every broker-provisioned login, i.e.
    the outage this story is most at risk of causing.
    """
    reader = _fake_reader(_NOT_ELIGIBLE)
    pre_signup.ALLOWLIST_MODE = "platform"
    event = {
        "triggerSource": "PreSignUp_AdminCreateUser",
        "userName": f"GitHub_{_GITHUB_ID}",
        "request": {"userAttributes": {"preferred_username": _LOGIN}},
        "response": {},
    }
    with patch.dict(sys.modules, {"membership_eligibility": reader}):
        result = pre_signup.handler(event, None)
    assert result is event
    reader.check_platform_membership.assert_not_called()
