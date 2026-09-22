"""Tests for account onboarding and vault credential endpoints."""

import uuid

import pytest

from app.middleware.auth import create_access_token


def _auth_header(org_id: uuid.UUID | None = None) -> dict:
    """Create an Authorization header with a valid JWT."""
    if org_id is None:
        org_id = uuid.uuid4()
    token, _ = create_access_token(org_id)
    return {"Authorization": f"Bearer {token}"}


class TestRegisterAccount:
    """Test POST /accounts."""

    @pytest.mark.asyncio
    async def test_register_requires_auth(self, client):
        """Registering an account without auth returns 401/403."""
        response = await client.post(
            "/accounts",
            json={
                "name": "test-account",
                "provider": "aws",
                "account_id": "123456789012",
                "role_arn": "arn:aws:iam::123456789012:role/SuperplaneAccess",
                "external_id": "sp-org-test1234",
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_register_validates_provider(self, client):
        """Invalid provider returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/accounts",
            json={
                "name": "test",
                "provider": "invalid_provider",
                "account_id": "123",
                "role_arn": "arn:aws:iam::123:role/Test",
                "external_id": "sp-test",
            },
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_register_validates_required_fields(self, client):
        """Missing required fields returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/accounts",
            json={"name": "test"},
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_register_validates_empty_name(self, client):
        """Empty name returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/accounts",
            json={
                "name": "",
                "provider": "aws",
                "account_id": "123456789012",
                "role_arn": "arn:aws:iam::123456789012:role/Test",
                "external_id": "sp-test",
            },
            headers=headers,
        )
        assert response.status_code == 422


class TestListAccounts:
    """Test GET /accounts."""

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        """Listing accounts without auth returns 401/403."""
        response = await client.get("/accounts")
        assert response.status_code in (401, 403)


class TestDeleteAccount:
    """Test DELETE /accounts/{id}."""

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, client):
        """Deleting an account without auth returns 401/403."""
        account_id = uuid.uuid4()
        response = await client.delete(f"/accounts/{account_id}")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client):
        """Deleting a non-existent account returns 404."""
        headers = _auth_header()
        account_id = uuid.uuid4()
        response = await client.delete(f"/accounts/{account_id}", headers=headers)
        assert response.status_code == 404


class TestRegisterCredential:
    """Test POST /vault/credentials."""

    @pytest.mark.asyncio
    async def test_register_aws_role_reference_from_gateway(self, client):
        from app.models.organization import Organization
        from tests.conftest import async_session_test

        org_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add(Organization(id=org_id, name="aws-role-reference"))
            await session.commit()
        headers = _auth_header(org_id)
        response = await client.post(
            "/vault/credentials",
            json={
                "name": "AWS role",
                "provider": "aws",
                "credential_type": "aws_role",
                "adp_credential_id": "adp-cred-role-reference",
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["credential_type"] == "aws_role"
        listing = await client.get("/vault/credentials", headers=headers)
        assert "adp-cred-role-reference" in listing.text
        assert "arn:" not in listing.text

    @pytest.mark.asyncio
    async def test_register_requires_auth(self, client):
        """Registering a credential without auth returns 401/403."""
        response = await client.post(
            "/vault/credentials",
            json={
                "name": "test-cred",
                "provider": "nebius",
                # Issue #5046 (U13b): the field is an ADP credential ID — an opaque vault
                # reference — not a Secrets Manager ARN.
                "adp_credential_id": "adp-cred-01HQ8V3XK2WERTY",
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_register_validates_required_fields(self, client):
        """Missing required fields returns 422."""
        headers = _auth_header()
        response = await client.post(
            "/vault/credentials",
            json={"name": "test"},
            headers=headers,
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_register_rejects_a_secret_arn_at_the_route(self, client):
        """Issue #5046 (U13b): an authenticated caller cannot register a secret ARN.

        The model-level rule is covered in test_models.py; this asserts it is reachable
        through the actual HTTP route, so a client sending the old ARN-shaped payload gets
        a 422 rather than persisting a second reference to secret material.
        """
        headers = _auth_header()
        response = await client.post(
            "/vault/credentials",
            json={
                "name": "test-cred",
                "provider": "nebius",
                "adp_credential_id": (
                    "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AbCdEf"
                ),
            },
            headers=headers,
        )
        assert response.status_code == 422
        assert "must not be an ARN" in response.text


class TestValidationErrorsDoNotEchoTheRejectedValue:
    """Issue #5053 (U7b): a 422 refusing secret material must not reproduce it.

    FastAPI's default handler puts each error's ``input`` in the response body
    verbatim, so before this story the route above — whose entire purpose is refusing
    a secret ARN — answered with the full ARN, AWS account id, region and secret name
    included. Measured, not theorized: the assertions below failed against the
    unpatched handler.

    Written against the real route rather than by calling the handler directly,
    because the defect was in the wiring: the validator's message was already safe
    and the leak came from a layer nobody had inspected.
    """

    # Not a credential for anything. Structurally complete so the ARN detector and
    # the account-id assertion below have something real to match; the account id is
    # the reserved all-zeros value and the secret does not exist.
    FAKE_ARN = (
        "arn:aws:secretsmanager:us-east-1:000000000000:secret:fake-not-real-AbCdEf"
    )

    @pytest.mark.asyncio
    async def test_422_body_does_not_contain_the_submitted_arn(self, client):
        """The rejected ARN appears nowhere in the response body."""
        response = await client.post(
            "/vault/credentials",
            json={
                "name": "test-cred",
                "provider": "nebius",
                "adp_credential_id": self.FAKE_ARN,
            },
            headers=_auth_header(),
        )

        assert response.status_code == 422
        assert self.FAKE_ARN not in response.text
        # Checked separately from the whole ARN: a partial echo that dropped the
        # prefix would still disclose the account, which is the part that turns an
        # over-broad IAM policy into a read of the secret.
        assert "000000000000" not in response.text
        assert "fake-not-real-AbCdEf" not in response.text
        assert "arn:aws:secretsmanager" not in response.text

    @pytest.mark.asyncio
    async def test_422_still_says_which_rule_was_broken(self, client):
        """Scrubbing keeps the explanation. A refusal nobody can act on is a defect too.

        The first version of this handler ran the message through ``scrub``, which
        withholds a whole value — collapsing the explanation to ``[REDACTED]`` and
        leaving the caller unable to tell a rejected ARN from a rejected empty string.
        This pins the span-level behaviour that replaced it.
        """
        response = await client.post(
            "/vault/credentials",
            json={
                "name": "test-cred",
                "provider": "nebius",
                "adp_credential_id": self.FAKE_ARN,
            },
            headers=_auth_header(),
        )

        assert response.status_code == 422
        assert "must not be an ARN" in response.text
        # The redaction is visible as a placeholder rather than as a silent deletion,
        # so an operator reading the body can tell something was withheld.
        assert "[REDACTED]" in response.text

    @pytest.mark.asyncio
    async def test_422_does_not_echo_a_secret_value_under_an_unknown_key(self, client):
        """An AWS key sent under a field we never declared is not echoed either.

        The handler is registered app-wide, not on the credential routes, because any
        field anywhere can be handed a secret by mistake and a per-route handler
        protects only the routes someone remembered to annotate. `AKIA` + 16 chars is
        the shape, matching no real key.
        """
        fake_key = "AKIA" + "Z" * 16
        response = await client.post(
            "/vault/credentials",
            json={
                "name": "test-cred",
                "provider": "nebius",
                "adp_credential_id": fake_key,
            },
            headers=_auth_header(),
        )

        assert response.status_code == 422
        assert fake_key not in response.text

    @pytest.mark.asyncio
    async def test_ordinary_422s_remain_debuggable(self, client):
        """Scrubbing is targeted, not a blanket suppression of validation errors.

        Deleting the errors wholesale would have fixed the leak by making every 422
        in the service undebuggable. A missing-field error still names the field and
        its error type.
        """
        response = await client.post(
            "/vault/credentials", json={"name": "test"}, headers=_auth_header()
        )

        assert response.status_code == 422
        body = response.json()
        assert isinstance(body["detail"], list) and body["detail"]
        assert "provider" in response.text
        # `input` is dropped from every entry — it is the caller's raw value, of
        # unknown provenance, and any part of it could be the secret.
        assert all("input" not in entry for entry in body["detail"])
        # ...but the diagnosis survives.
        assert all("type" in entry and "loc" in entry for entry in body["detail"])


class TestListCredentials:
    """Test GET /vault/credentials."""

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        """Listing credentials without auth returns 401/403."""
        response = await client.get("/vault/credentials")
        assert response.status_code in (401, 403)


class TestDeleteCredential:
    """Test DELETE /vault/credentials/{id}."""

    @pytest.mark.asyncio
    async def test_delete_requires_auth(self, client):
        """Deleting a credential without auth returns 401/403."""
        cred_id = uuid.uuid4()
        response = await client.delete(f"/vault/credentials/{cred_id}")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_404(self, client):
        """Deleting a non-existent credential returns 404."""
        headers = _auth_header()
        cred_id = uuid.uuid4()
        response = await client.delete(f"/vault/credentials/{cred_id}", headers=headers)
        assert response.status_code == 404
