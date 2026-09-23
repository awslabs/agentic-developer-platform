"""The magic-link signing key is independent of the session-signing key (#5656, A05).

The gateway mints short-lived single-use "magic link" tokens that let someone
prove a chat identity (Slack, GitHub, WhatsApp, Discord) belongs to their
account. Those tokens were signed with `magic_link_secret or token_secret_key` —
so whenever the dedicated key was unset, the identity-linking tokens were signed
with the key that signs every platform session. In practice no deployment path
set `BG_MAGIC_LINK_SECRET` at all, so every environment ran on that fallback.

Two consequences, and this suite pins the fix for both:

  1. **Neither key could be replaced.** Containing a leaked magic-link token
     meant rotating the session key, which signs out every user on the platform.
     Conversely, a session-key rotation silently broke in-flight identity
     linking. One symmetric key signing two purposes is one blast radius, and an
     issuer ("iss") claim check does not change that — it constrains what a
     *verifier* accepts, not what rotating the key destroys.
  2. **The gap was invisible.** An environment looked configured while running
     on the fallback, so an operator had no signal that the separation they
     believed in did not exist.

Both resolvers are tested, not just one. `src/auth/vault_routes.py` (the
operator-facing endpoint) and `src/internal/routes.py` (the endpoint ingest
Lambdas call) each carried their own copy of the fallback; had only one been
fixed, the internal path would have kept minting tokens under the session key
while the other refused — the worst outcome, since the surface stays open while
the issue reads as closed.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import MagicMock, patch

import jwt
import pytest

from src.auth.magic_link import TokenInvalidError, issue_token, verify_token

# Two deliberately different keys. The point of the suite is that a token signed
# under one is not accepted under the other, so these must never be equal.
_MAGIC_LINK_KEY = "magic-link-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_SESSION_KEY = "session-signing-key-bbbbbbbbbbbbbbbbbbbb"

# Every resolver that answers "which key signs a magic link?". Both are asserted
# by every test below, because the fallback existed in both.
_RESOLVERS = [
    pytest.param("src.auth.vault_routes", id="vault_routes"),
    pytest.param("src.internal.routes", id="internal_routes"),
]


def _resolve(module_path: str, *, magic_link_secret: str, token_secret_key: str) -> str:
    """Call a module's _get_magic_link_secret with the two keys configured."""
    module = __import__(module_path, fromlist=["_get_magic_link_secret"])
    settings = MagicMock()
    settings.magic_link_secret = magic_link_secret
    settings.token_secret_key = token_secret_key
    with patch(f"{module_path}.get_settings", return_value=settings):
        return module._get_magic_link_secret()


class TestNoFallbackToSessionKey:
    """An unset magic-link key must not resolve to the session key."""

    @pytest.mark.parametrize("module_path", _RESOLVERS)
    def test_configured_key_is_used(self, module_path):
        assert _resolve(module_path, magic_link_secret=_MAGIC_LINK_KEY, token_secret_key=_SESSION_KEY) == _MAGIC_LINK_KEY

    @pytest.mark.parametrize("module_path", _RESOLVERS)
    def test_unset_key_does_not_borrow_the_session_key(self, module_path):
        """This is the regression. Previously this returned the session key."""
        resolved = _resolve(module_path, magic_link_secret="", token_secret_key=_SESSION_KEY)
        assert resolved != _SESSION_KEY, "magic-link signing must not fall back to the session-signing key"
        assert resolved == "", "an unset key must resolve to empty so callers answer 503 not_configured"

    @pytest.mark.parametrize("module_path", _RESOLVERS)
    def test_the_two_keys_never_resolve_to_one_value(self, module_path):
        """Whatever the configuration, the magic-link key is never the session key."""
        for magic_link_secret in (_MAGIC_LINK_KEY, ""):
            resolved = _resolve(module_path, magic_link_secret=magic_link_secret, token_secret_key=_SESSION_KEY)
            assert resolved != _SESSION_KEY

    @pytest.mark.parametrize("module_path", _RESOLVERS)
    def test_no_fallback_expression_survives_in_source(self, module_path):
        """Regression guard against the `or token_secret_key` fallback returning.

        Asserted on source because a reintroduced fallback is invisible in any
        environment that *does* configure both keys — exactly the environment a
        test fixture sets up — so behaviour alone would not catch it coming back
        in a form the parametrised cases above happen not to cover.

        The function's own docstring explains what it must NOT do and therefore
        names the session key, so the check is scoped to executable lines: the
        docstring is stripped and comment lines skipped. Checking raw text would
        force the fix to be documented without naming what it fixed.
        """
        module = __import__(module_path, fromlist=["_get_magic_link_secret"])
        source = inspect.getsource(module._get_magic_link_secret)

        tree = ast.parse(textwrap.dedent(source))
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        if ast.get_docstring(func) is not None:
            func.body = func.body[1:]  # drop the docstring node
        code_only = ast.unparse(func)

        assert "token_secret_key" not in code_only, (
            f"{module_path}._get_magic_link_secret must not reference the session-signing key in code; got:\n{code_only}"
        )


class TestIndependentRotation:
    """Each key can be replaced without disturbing the other.

    This is the property the separation exists to provide, so it is asserted
    directly rather than inferred from the resolvers: rotate one key, and tokens
    under the *other* must keep verifying.
    """

    @staticmethod
    def _issue(secret: str) -> str:
        return issue_token(
            provider="slack",
            provider_user_id="U123",
            channel_context="T01/C02",
            target_user_id="user-abc",
            secret_key=secret,
        )["token"]

    def test_rotating_the_magic_link_key_leaves_sessions_intact(self):
        """Replace the magic-link key: its tokens stop verifying, sessions do not."""
        link_token = self._issue(_MAGIC_LINK_KEY)
        session_token = jwt.encode({"sub": "user-abc"}, _SESSION_KEY, algorithm="HS256")

        rotated_magic_link_key = _MAGIC_LINK_KEY + "-rotated"
        with pytest.raises(TokenInvalidError):
            verify_token(link_token, rotated_magic_link_key)

        # The session key was untouched, so sessions established before the
        # magic-link rotation keep working. Under the old fallback this token
        # would have been signed with the same key and died with it.
        assert jwt.decode(session_token, _SESSION_KEY, algorithms=["HS256"])["sub"] == "user-abc"

    def test_rotating_the_session_key_leaves_identity_linking_intact(self):
        """The converse: a session-key rotation must not break in-flight links."""
        link_token = self._issue(_MAGIC_LINK_KEY)
        session_token = jwt.encode({"sub": "user-abc"}, _SESSION_KEY, algorithm="HS256")

        rotated_session_key = _SESSION_KEY + "-rotated"
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(session_token, rotated_session_key, algorithms=["HS256"])

        # Identity linking is unaffected — the demonstration that the two
        # purposes are independently rotatable.
        assert verify_token(link_token, _MAGIC_LINK_KEY)["provider_user_id"] == "U123"

    def test_a_magic_link_token_is_not_accepted_under_the_session_key(self):
        """Cross-key rejection: the keys are not interchangeable in either direction."""
        link_token = self._issue(_MAGIC_LINK_KEY)
        with pytest.raises(TokenInvalidError):
            verify_token(link_token, _SESSION_KEY)

    def test_a_token_signed_with_the_session_key_is_not_accepted_as_a_magic_link(self):
        session_signed = self._issue(_SESSION_KEY)
        with pytest.raises(TokenInvalidError):
            verify_token(session_signed, _MAGIC_LINK_KEY)


class TestSettingsHasNoBuiltInDefault:
    """The key must come from configuration, never from a literal in source."""

    def test_magic_link_secret_defaults_to_empty(self):
        from src.shared.config import Settings

        field = Settings.model_fields["magic_link_secret"]
        assert field.default == "", "magic_link_secret must not ship a usable default value"
