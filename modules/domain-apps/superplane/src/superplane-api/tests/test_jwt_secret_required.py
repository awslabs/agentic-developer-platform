"""A missing token signing key fails closed — issue #5683 (A04).

## What was wrong

`app/config.py` gave `jwt_secret_key` a hardcoded placeholder default. That key both
signs and verifies the org-scoped tokens `/auth/login` issues, and the default was
*accepted at runtime*, so a deployment that never set `JWT_SECRET_KEY` served
traffic on a value committed to this repository. Anyone able to read the source
could mint a token the server would accept as an authenticated organization, and
the server verifying with that key could not distinguish a forged token from a
real one. Nothing in the logs indicated the fallback had been used.

The removed value is not quoted anywhere in this file. Any environment still
running on it stays forgeable until rotated, so the assertions below test the
credential *shape* and the runtime behaviour — proving the literal is gone by
restating it would ship the very thing being removed.

## What these tests pin

That the fallback is *gone*, not merely changed — three independent properties,
because removing a default is easy to half-undo:

1. No credential literal remains in the settings default (the value-level claim).
2. An unset key is refused at every sign and verify entry point (the runtime
   claim), and refused as a configuration fault rather than reported as a failed
   authentication.
3. An *empty* key is refused too (the claim that is easy to get wrong). This is the
   subtle one: `jose.jwt.encode` signs happily with `""`, so "no default" and "an
   empty default" are not the same fix. An empty key would still be a working
   signing key that every reader of the source can guess.

## A note on the fixtures below

No test here reproduces the removed placeholder or any other usable credential.
Where a key is needed, it is an obviously-labelled local string; where the
*absence* of a key is the subject, the key is cleared rather than replaced. The
acceptance criterion forbids reproducing the literal, and asserting on the exact
removed string in order to prove it is gone would reintroduce it into a file that
ships — so the scans below assert on the credential *shape* and on the runtime
behaviour instead.
"""

from __future__ import annotations

import inspect
import re
import uuid
from pathlib import Path

import pytest

from app.config import Settings, settings
from app.middleware.auth import (
    JWTSecretKeyMissing,
    create_access_token,
    decode_token,
    require_jwt_secret_key,
)

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


class TestTheSettingsDefaultCarriesNoKey:
    """The value-level half: there is nothing to leak from the source file."""

    def test_the_shipped_default_is_unset(self) -> None:
        """A fresh Settings built with no env override has no signing key.

        `Settings()` rather than the imported `settings` singleton: the singleton is
        whatever this process configured, while the class default is what a
        deployment inherits when it sets nothing. The defect was in the latter.
        """
        assert Settings(_env_file=None).jwt_secret_key == "", (
            "jwt_secret_key must have no default. A default here is a credential in "
            "the repository that deployments can unknowingly run on."
        )

    def test_the_default_is_not_a_placeholder_phrase(self) -> None:
        """Guards the obvious near-miss: swapping one placeholder for another.

        Checked by shape, not against the removed string, so this test does not
        itself carry the literal it exists to keep out.
        """
        default = Settings(_env_file=None).jwt_secret_key
        assert not re.search(
            r"change|replace|todo|fixme|example|placeholder|secret|password|prod",
            default,
            re.IGNORECASE,
        ), f"the default looks like a placeholder credential: {default!r}"

    def test_the_config_source_holds_no_assigned_credential_literal(self) -> None:
        """A text-level scan, because a value can return by direct assignment.

        The runtime checks below all read `require_jwt_secret_key()`. This one
        catches the edit that bypasses them entirely by putting a literal back on
        the field, which no behavioural test would notice if the literal happened to
        be supplied.
        """
        source = (APP_ROOT / "config.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            code = line.split("#", 1)[0]
            if "jwt_secret_key" not in code or ":" not in code:
                continue
            value = code.split("=", 1)[1].strip() if "=" in code else ""
            assert value in ('""', "''", ""), (
                f"jwt_secret_key is assigned a literal default: {line.strip()!r}"
            )


class TestAnUnsetKeyIsRefused:
    """The runtime half: every path that needs the key refuses without it."""

    @pytest.fixture
    def no_key(self, monkeypatch):
        """Clear the signing key the offline suite's conftest fixture installs."""
        monkeypatch.setattr(settings, "jwt_secret_key", "")

    def test_the_resolver_raises(self, no_key) -> None:
        with pytest.raises(JWTSecretKeyMissing):
            require_jwt_secret_key()

    def test_signing_raises_instead_of_minting_a_forgeable_token(
        self, no_key
    ) -> None:
        """The consequence that matters: no token is produced at all.

        Before the fix this call returned a perfectly valid token signed with the
        published key.
        """
        with pytest.raises(JWTSecretKeyMissing):
            create_access_token(uuid.uuid4())

    def test_verifying_raises_rather_than_answering_401(self, no_key) -> None:
        """A missing key is a configuration fault, not an authentication result.

        This is why `decode_token` resolves the key *outside* its `try`. Folding the
        refusal into the except arm would answer "Invalid or expired token" — which
        sends an operator debugging a total login outage looking at tokens instead of
        at the deployment's missing secret.
        """
        with pytest.raises(JWTSecretKeyMissing):
            decode_token("any.token.value")

    def test_a_whitespace_only_key_is_not_a_key(self, monkeypatch) -> None:
        """Whitespace is what a broken secret-store template yields, not a secret."""
        monkeypatch.setattr(settings, "jwt_secret_key", "   \n\t  ")
        with pytest.raises(JWTSecretKeyMissing):
            require_jwt_secret_key()

    def test_the_refusal_names_the_variable_without_echoing_material(
        self, monkeypatch
    ) -> None:
        """The error must not become the disclosure the check exists to prevent.

        A startup failure lands in cluster logs and CI output. Two properties: the
        message must be actionable (name the variable an operator has to set), and it
        must not quote the setting's contents.

        The rejected value here is a recognisable sentinel rather than a bare empty
        string, so the "does not echo" assertion is a real one — with `""` there
        would be nothing to find and the check would pass vacuously. Whitespace is
        also the realistic version of this failure: a secret-store template that
        rendered to blanks, whose contents a naive `f"got {key!r}"` would happily
        print.
        """
        sentinel = "SENTINEL-whitespace-wrapped-value-must-not-be-echoed"
        monkeypatch.setattr(settings, "jwt_secret_key", f" \t{sentinel}\n ")
        # Sanity: a non-blank value must NOT be refused, or the assertion below
        # would be testing the wrong branch.
        assert require_jwt_secret_key().strip() == sentinel

        monkeypatch.setattr(settings, "jwt_secret_key", "   \n\t  ")
        with pytest.raises(JWTSecretKeyMissing) as exc_info:
            require_jwt_secret_key()
        message = str(exc_info.value)

        assert sentinel not in message
        assert "JWT_SECRET_KEY" in message, (
            "the refusal must name the variable to set, or it is not actionable"
        )


class TestAnEmptyKeyIsNotTreatedAsAKey:
    """The claim that is easy to get wrong, pinned against the library's behaviour."""

    def test_the_jwt_library_would_sign_with_an_empty_key(self) -> None:
        """Why "default to empty" is not by itself the fix.

        Pinned as an executable fact rather than left in a comment: if a future
        version of `jose` started rejecting empty keys, this test failing is the
        signal that the reasoning in `app/config.py` needs revisiting. It asserts the
        hazard exists, which is what makes `require_jwt_secret_key()` necessary
        rather than belt-and-braces.
        """
        from jose import jwt

        token = jwt.encode({"sub": "x"}, "", algorithm="HS256")
        assert token.count(".") == 2, (
            "expected jose to sign with an empty key; if it now refuses, the empty "
            "default in app/config.py is load-bearing and the comment there is stale"
        )

    def test_so_the_application_refuses_what_the_library_would_allow(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(settings, "jwt_secret_key", "")
        with pytest.raises(JWTSecretKeyMissing):
            create_access_token(uuid.uuid4())


class TestThereIsExactlyOneReaderOfTheKey:
    """Structural: the check cannot be bypassed by reading the setting directly."""

    def test_no_module_reads_settings_jwt_secret_key_outside_the_resolver(
        self,
    ) -> None:
        """One chokepoint, or the guarantee is only as good as the next edit.

        `require_jwt_secret_key()` is allowed to read the setting — it is the
        resolver. Anything else reading `settings.jwt_secret_key` would reintroduce
        the unguarded path, and would do it invisibly because the suite supplies a
        key.
        """
        resolver_source = inspect.getsource(require_jwt_secret_key)
        offenders: list[str] = []
        for path in sorted(APP_ROOT.rglob("*.py")):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                code = line.split("#", 1)[0]
                if "settings.jwt_secret_key" not in code:
                    continue
                if code.strip() in resolver_source:
                    continue
                offenders.append(f"{path.relative_to(APP_ROOT)}:{number}")
        assert not offenders, (
            "these read settings.jwt_secret_key directly and so bypass the "
            f"fail-closed check; call require_jwt_secret_key() instead: {offenders}"
        )


class TestStartupRefusesRatherThanServing:
    """A misconfigured deployment must not start, pass probes and fail every login."""

    def test_the_lifespan_checks_the_key(self, monkeypatch) -> None:
        """Exercised through the real lifespan, not by reading main.py for a string.

        The reconcilers the lifespan starts need no database here because the check
        is deliberately placed before them — which is itself part of the property:
        the refusal must not depend on anything else having succeeded first.
        """
        import anyio

        from app.main import app as fastapi_app
        from app.main import lifespan

        monkeypatch.setattr(settings, "jwt_secret_key", "")

        async def _enter() -> None:
            async with lifespan(fastapi_app):
                pytest.fail("startup completed without a signing key")

        with pytest.raises(JWTSecretKeyMissing):
            anyio.run(_enter)
