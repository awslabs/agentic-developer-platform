"""Tests verifying all SQLAlchemy models are importable and have correct table names."""

import json

import pytest

import app.models  # noqa: F401 — triggers model registration with Base
from app.database import Base


def test_all_tables_registered():
    """All tables from design doc sections 6.1, 15.7, and auth are registered."""
    expected_tables = {
        "organizations",
        "organization_grants",
        "workspaces",
        "clusters",
        "node_pools",
        "nodes",
        "deployments",
        "events",
        "credential_registry",
        "cluster_vault_assignments",
        "credential_audit_log",
        "cloud_accounts",
        "reconcile_locks",
        "api_keys",
        "research_findings",
        "research_proposals",
        "budget_alerts",
        "users",
        # Receiver-side state for the observation contract (issue #5056, U15).
        # Deliberately not columns on `reconcile_locks`: U15's point is that the
        # monitor stops writing to domain tables, so lease state belongs to the
        # receiver that grants it.
        "observation_receipts",
        "observation_leases",
        # Per-workspace authorization grants (issue #5055, U14 — R6). The record
        # that a named principal may act on one workspace. Before it there was no
        # schema able to express that, so authority was the caller's organization
        # and every org-mate reached every workspace in it.
        "workspace_grants",
        # Provider connections and their workspace bindings (issue #5053, U7b —
        # R7). Two tables, not one, because "who may delegate this credential"
        # and "which workspace may use it" are the two checks the contract
        # refuses to let stand in for each other.
        "provider_connections",
        "provider_connection_bindings",
        # Durable provider-operation records (issue #5054, U11c — R15). One row per
        # idempotency identity, written BEFORE the provider call it identifies, so a
        # response lost to a timeout or a crash still has something to reconcile
        # against. Without it the handle contract's "recorded before the call counts
        # as made" rule has no storage to be true in.
        "provider_operations",
        "provider_allocations",
        "provider_reference_conflicts",
        "provider_allocation_resources",
        # PostgreSQL journals used by the domain bootstrap SQL adapters.
        "workspace_bootstrap_reservations",
        "workspace_bootstrap_authority",
        # The budget ledger's journal (issue #5535). Registered here — rather than
        # existing only in migration 018 — so Alembic autogeneration compares against
        # it and the declared schema cannot drift from the migration. Its rows are
        # written by raw SQL over the harness connection, not through a session; see
        # `app/models/operation_budget.py`.
        "operation_budget_reservations",
    }
    actual_tables = set(Base.metadata.tables.keys())
    assert expected_tables == actual_tables, (
        f"Missing: {expected_tables - actual_tables}, Extra: {actual_tables - expected_tables}"
    )


def test_organization_columns():
    """Organization table has required columns."""
    table = Base.metadata.tables["organizations"]
    col_names = {c.name for c in table.columns}
    assert {
        "id",
        "name",
        "cognito_sub",
        "billing_plan",
        "quotas_json",
        "created_at",
    } <= col_names


def test_workspace_has_isolation_mode():
    """Workspace table includes isolation_mode from section 15.7."""
    table = Base.metadata.tables["workspaces"]
    col_names = {c.name for c in table.columns}
    assert "isolation_mode" in col_names
    assert "shared_cluster_id" in col_names
    assert "namespace_name" in col_names


def test_cluster_has_heartbeat_fields():
    """Cluster table has endpoint, health_status, last_heartbeat from issue requirements."""
    table = Base.metadata.tables["clusters"]
    col_names = {c.name for c in table.columns}
    assert {"endpoint", "health_status", "last_heartbeat"} <= col_names


class TestCredentialRecordsHoldAReferenceNotSecretMaterial:
    """Issue #5046 (U13b), R7 schema half — the schema rule and its enforcement.

    THE RULE: a domain record stores an **ADP credential ID** (an opaque handle only the
    ADP vault can resolve) and holds no secret value and no copied secret ARN.

    WHY THE PREVIOUS TEST WAS NOT ENOUGH. It asserted `secret_arn in col_names` and
    `secret_value not in col_names`, and it passed. What it established was narrow: that
    this table holds no secret *values*. It did not establish that the vault is the single
    owner of the material. A secret's ARN is its address in Secrets Manager, so a copy of
    it is a second, independent route to the secret living outside the vault — the vault
    could rotate that credential to a new value or revoke it and nothing would reach the
    copy. `vault_sync.py` wrote this column into a workload cluster's ExternalSecret, so
    the cluster read the secret directly and the vault never saw it.

    WHAT THESE TESTS DO NOT ESTABLISH, stated because it is the issue's central caution:
    they are offline static checks of a reference's *shape*. Nothing here shows that a
    well-formed `adp_credential_id` is *resolvable* — that ADP owns that credential, that
    account and KMS permissions let ADP read it, or that rotation and revocation reach it.
    Shape and resolvability are separate facts. The second is verified only by the audited
    vault-owned migration (R7 acceptances 6-7), which is deferred live work under U7 and
    is NOT closed by this story. "ARN-free" is necessary, not sufficient.
    """

    def test_credential_registry_holds_no_secret_value_and_no_secret_arn(self):
        """The absence check the issue asks for, plus the reference that replaced it."""
        table = Base.metadata.tables["credential_registry"]
        col_names = {c.name for c in table.columns}

        assert "adp_credential_id" in col_names, (
            "credential_registry must reference the credential by ADP credential ID"
        )
        assert "secret_value" not in col_names, "a secret value must never be stored"
        assert "secret_arn" not in col_names, (
            "credential_registry must not hold a secret ARN: a copied secret address is a "
            "second reference to secret material outside the ADP vault, so vault rotation "
            "and revocation would no longer reach it"
        )
        assert "kms_key_id" not in col_names, (
            "kms_key_id existed only to decrypt a secret this record no longer resolves; "
            "keeping the decryption key is the same leak in a different column"
        )

    def test_cloud_accounts_holds_no_secret_arn_list(self):
        table = Base.metadata.tables["cloud_accounts"]
        col_names = {c.name for c in table.columns}
        assert "adp_credential_ids_json" in col_names
        assert "secret_arns_json" not in col_names, (
            "cloud_accounts must reference credentials by ADP credential ID, not by a "
            "list of copied secret ARNs"
        )

    def test_no_model_field_is_named_to_hold_a_secret_arn(self):
        """No model field anywhere is typed or named to hold a secret ARN.

        Scans every registered table rather than only the two this story edits, so a new
        model reintroducing a secret-ARN column fails here instead of shipping.

        IAM **role** ARN columns are deliberately allowed. A role ARN names *who may act* —
        it is an identity, carries no secret value, and grants nothing by itself without a
        trust policy admitting the caller. Dropping `cross_account_role_arn` /
        `ingest_role_arn` / `irsa_role_arns_json` / `agent_iam_role_arn` would break
        cross-account role assumption while protecting nothing. The distinction this test
        enforces is secret-material references, not the substring "arn".
        """
        offenders: list[str] = []
        for table_name, table in Base.metadata.tables.items():
            for column in table.columns:
                name = column.name.lower()
                if "role_arn" in name:  # an identity, not secret material
                    continue
                if "secret" in name and "arn" in name:
                    offenders.append(f"{table_name}.{column.name}")

        assert not offenders, (
            f"model fields named to hold a secret ARN: {offenders}. Store an opaque ADP "
            f"credential ID instead, so the ADP vault stays the only resolver and "
            f"rotation/revocation keep reaching the credential."
        )

    def test_no_model_field_is_named_to_hold_a_secret_value(self):
        """A record must never have somewhere to put the credential itself."""
        offenders: list[str] = []
        for table_name, table in Base.metadata.tables.items():
            for column in table.columns:
                name = column.name.lower()
                if name in {
                    "secret_value",
                    "secret",
                    "api_key",
                    "credential_value",
                    "private_key",
                    "password",
                }:
                    offenders.append(f"{table_name}.{column.name}")

        assert not offenders, (
            f"model fields able to hold credential material: {offenders}. Secret values "
            f"belong only in the ADP vault."
        )


class TestCredentialReferenceRejectsArnsAndSecrets:
    """The rename is enforced, not merely declared.

    Renaming the column to `adp_credential_id` does not by itself stop an ARN being
    stored: it is still a string column, and an ARN fits it exactly as well as it fit
    `secret_arn`. Without a check, the original defect returns under a compliant column
    name — which is the "ARN-free treated as sufficient" failure mode in the issue's risk
    table. These tests pin the check that makes the rename mean something.

    Asserted through the model (not only the request schema) because the model is the
    boundary every writer crosses — a router, a reconciler, a backfill script or a test
    fixture — rather than only traffic arriving through the validated API.
    """

    def test_accepts_an_opaque_adp_credential_id(self):
        from app.models.credential import CredentialRegistry

        cred = CredentialRegistry(adp_credential_id="adp-cred-01HQ8V3XK2WERTY")
        assert cred.adp_credential_id == "adp-cred-01HQ8V3XK2WERTY"

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-key-AbCdEf",
            # Non-commercial partitions are still ARNs; matching the `arn:` scheme rather
            # than enumerating partitions is what makes these fail too.
            "arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:secret:k-AbCdEf",
            "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:k-AbCdEf",
            # A KMS key ARN and an SSM parameter ARN address material this record must not
            # name either, so the rule is not secretsmanager-specific.
            "arn:aws:kms:us-east-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab",
            "arn:aws:ssm:us-east-1:123456789012:parameter/nebius/api-key",
            "ARN:AWS:SECRETSMANAGER:us-east-1:123456789012:secret:upper-AbCdEf",
        ],
    )
    def test_rejects_an_arn_shaped_string(self, arn):
        """An ARN-shaped value must be refused, whatever the column is called."""
        from app.models.credential import CredentialRegistry

        with pytest.raises(ValueError, match="must not be an ARN"):
            CredentialRegistry(adp_credential_id=arn)

    def test_rejects_key_material(self):
        from app.models.credential import CredentialRegistry

        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
        with pytest.raises(ValueError, match="key material"):
            CredentialRegistry(adp_credential_id=pem)

    def test_rejects_a_value_too_long_to_be_a_reference(self):
        """A long blob is secret material, not a handle.

        The column is VARCHAR(255), so PostgreSQL would reject this anyway with a 22001
        truncation error — but SQLite (which these tests run on) silently accepts an
        oversized string, and the resulting error would name a width, not the reason.
        """
        from app.models.credential import CredentialRegistry

        with pytest.raises(ValueError, match="short opaque reference"):
            CredentialRegistry(adp_credential_id="k" * 300)

    @pytest.mark.parametrize("empty", ["", "   ", "\t"])
    def test_rejects_an_empty_reference(self, empty):
        """An empty reference resolves to nothing, so it is not a valid record."""
        from app.models.credential import CredentialRegistry

        with pytest.raises(ValueError, match="non-empty"):
            CredentialRegistry(adp_credential_id=empty)

    def test_the_api_boundary_rejects_an_arn_too(self):
        """The request schema shares the model's rule, so an ARN is a 422, not a 500."""
        import pydantic

        from app.schemas.account import RegisterCredentialRequest

        with pytest.raises(pydantic.ValidationError, match="must not be an ARN"):
            RegisterCredentialRequest(
                name="nebius-prod",
                provider="nebius",
                adp_credential_id=(
                    "arn:aws:secretsmanager:us-east-1:123456789012:secret:k-AbCdEf"
                ),
            )

    def test_the_api_boundary_accepts_a_reference(self):
        from app.schemas.account import RegisterCredentialRequest

        body = RegisterCredentialRequest(
            name="nebius-prod",
            provider="nebius",
            adp_credential_id="adp-cred-01HQ8V3XK2WERTY",
        )
        assert body.adp_credential_id == "adp-cred-01HQ8V3XK2WERTY"

    # --- the list column: cloud_accounts.adp_credential_ids_json ---
    #
    # Found in review of this PR. The singular column was guarded on the model and at the
    # API boundary while the LIST form -- the same rule, the other table the issue names --
    # was guarded at neither, so a caller could store a copied secret ARN or a PEM block on
    # `cloud_accounts` under the new compliant-looking column name. Nothing resolves that
    # column today, so it was a latent gap and not a live leak, but it left the story's rule
    # true only of the sibling table.

    ARN_IN_A_LIST = "arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"

    def test_the_list_column_rejects_an_arn(self):
        from app.models.cloud_account import CloudAccount

        with pytest.raises(ValueError, match="must not be an ARN"):
            CloudAccount(adp_credential_ids_json=json.dumps([self.ARN_IN_A_LIST]))

    def test_the_list_column_rejects_an_arn_hidden_among_valid_references(self):
        """Every element is checked, not just the first."""
        from app.models.cloud_account import CloudAccount

        with pytest.raises(ValueError, match="must not be an ARN"):
            CloudAccount(
                adp_credential_ids_json=json.dumps(
                    ["adp-cred-01HQ8V3XK2WERTY", self.ARN_IN_A_LIST]
                )
            )

    def test_the_list_column_rejects_an_interior_laundered_arn(self):
        """The interior-laundering fix must reach BOTH columns, not just the singular one.

        Both layers call one shared validator, so this asserts the arrangement actually
        holds rather than assuming it: an interior-spaced and a percent-encoded ARN were
        each accepted by this column and by the API boundary before the fix.
        """
        from app.models.cloud_account import CloudAccount

        for value in (
            "arn:aws:secretsmanager:us-east-1 :123456789012:secret:nebius-AbCdEf",
            self.ARN_IN_A_LIST.replace(":", "%3A"),
        ):
            with pytest.raises(ValueError, match="must not be an ARN"):
                CloudAccount(adp_credential_ids_json=json.dumps([value]))

    def test_the_list_column_rejects_key_material(self):
        from app.models.cloud_account import CloudAccount

        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA..."
        with pytest.raises(ValueError, match="key material"):
            CloudAccount(adp_credential_ids_json=json.dumps([pem]))

    def test_the_list_column_accepts_references_and_an_empty_list(self):
        """The guard must not break the legitimate shapes this column is for."""
        from app.models.cloud_account import CloudAccount

        payload = json.dumps(["adp-cred-01HQ8V3XK2WERTY", "adp-cred-01HQ8V3XK2QWER"])
        assert CloudAccount(adp_credential_ids_json=payload)
        assert CloudAccount(adp_credential_ids_json=json.dumps([]))
        # NULL means "no credentials referenced", which is a valid record.
        assert CloudAccount(adp_credential_ids_json=None)

    def test_the_list_api_boundary_rejects_an_arn(self):
        """A list payload is a 422 naming the field, not a 500 out of the model hook."""
        import pydantic

        from app.schemas.account import RegisterAccountRequest

        with pytest.raises(pydantic.ValidationError, match="must not be an ARN"):
            RegisterAccountRequest(
                name="prod",
                account_id="123456789012",
                role_arn="arn:aws:iam::123456789012:role/SuperplaneCrossAccount",
                external_id="ext-123",
                adp_credential_ids=[self.ARN_IN_A_LIST],
            )

    def test_the_list_api_boundary_keeps_accepting_iam_role_arns(self):
        """The guard must not spread to role ARNs: an identity is not secret material.

        `role_arn` and `irsa_role_arns` are ARNs by design and must stay accepted, or
        cross-account assumption breaks while nothing is protected.
        """
        from app.schemas.account import RegisterAccountRequest

        body = RegisterAccountRequest(
            name="prod",
            account_id="123456789012",
            role_arn="arn:aws:iam::123456789012:role/SuperplaneCrossAccount",
            external_id="ext-123",
            ingest_role_arn="arn:aws:iam::123456789012:role/SuperplaneIngest",
            adp_credential_ids=["adp-cred-01HQ8V3XK2WERTY"],
            irsa_role_arns=["arn:aws:iam::123456789012:role/SuperplaneIrsa"],
        )
        assert body.role_arn.startswith("arn:aws:iam::")
        assert body.irsa_role_arns == ["arn:aws:iam::123456789012:role/SuperplaneIrsa"]


class TestTheReferenceRuleCannotBeSteppedAround:
    """The rejection must hold against the ways a real value gets past a naive check.

    The story's own claim is that "a rename is not enforcement" — the validator is what
    makes the renamed column mean something. That argument only holds if the validator
    refuses the values it exists to refuse, so each case below is a value that the first
    implementation of this rule accepted:

    * an ARN is recognised by *searching* the string, so a leading zero-width character or
      a `"cred <arn>"` prefix no longer hides it from a `startswith` test while leaving the
      address perfectly usable to anything that trims or ignores the extra character;
    * the credentials most likely to be pasted into this column (an AWS access key id, a
      GitHub PAT, a Slack token, a provider API key) are all far shorter than the
      VARCHAR(255) limit, so a length-only rule accepted every one of them.

    These are model-layer tests, so they cover every writer — router, reconciler, backfill
    and fixture alike — rather than only traffic arriving through the validated API.
    """

    # Values that must never be storable. Each is a realistic paste, not a fuzz string.
    # The token bodies are synthetic and match no real credential.
    SECRET_SHAPED = {
        "zero_width_prefixed_arn": (
            "​arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"
        ),
        "arn_after_a_label": (
            "cred arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"
        ),
        "arn_after_a_newline": (
            "reference\narn:aws:secretsmanager:us-east-1:123456789012:secret:k-AbCdEf"
        ),
        # A leading *word* character suppresses a `\b`-anchored pattern completely, so these
        # escaped the first hardening attempt as well. `secret_arn:<value>` is the realistic
        # one: it is the old column name joined to its old value, which is exactly what a
        # copy-paste or a naive `f"{field}:{value}"` produces during this rename.
        "word_prefixed_arn": (
            "_arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"
        ),
        "old_column_name_prefixed_arn": (
            "secret_arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"
        ),
        # U+3164 HANGUL FILLER: renders blank, but Python classes it as a word character,
        # so it both suppresses `\b` and is not a "whitespace" or C0/zero-width character.
        "hangul_filler_prefixed_arn": (
            "ㅤarn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"
        ),
        "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "aws_session_key_id": "ASIAIOSFODNN7EXAMPLE",
        "word_prefixed_access_key": "_AKIAIOSFODNN7EXAMPLE",
        "word_suffixed_access_key": "AKIAIOSFODNN7EXAMPLE_",
        # The ASCII-alphanumeric affix class. A negative lookbehind of
        # `(?<![A-Za-z0-9])` is suppressed by exactly these, so the corpus above (all
        # separators and non-ASCII word characters) passed while a plain `x` still
        # laundered the credential through every layer.
        "alnum_prefixed_access_key": "xAKIAIOSFODNN7EXAMPLE",
        "alnum_suffixed_access_key": "AKIAIOSFODNN7EXAMPLEx",
        "word_run_prefixed_access_key": "awsAKIAIOSFODNN7EXAMPLE",
        "alnum_prefixed_session_key": "xASIAIOSFODNN7EXAMPLE",
        "alnum_prefixed_pat": "xghp_" + "a" * 36,
        "alnum_prefixed_slack_token": "xxoxb-123456789012-abcdefghijklmnop",
        "alnum_prefixed_provider_key": "xsk-ant-api03-" + "x" * 40,
        "alnum_prefixed_openai_style_key": "xsk-" + "A" * 40,
        # A value that normalizes back into a byte-exact ARN.
        "fullwidth_colon_arn": (
            "arn：aws：secretsmanager：us-east-1：123456789012：secret：nebius-AbCdEf"
        ),
        # ASCII separators in the service segment, which a `[a-z0-9-]` class missed.
        "underscored_service_arn": "arn:aws:secrets_manager:us-east-1:1:secret:nebius",
        "dotted_service_arn": "arn:aws:secrets.manager:us-east-1:1:secret:nebius",
        "word_prefixed_pat": "_ghp_" + "a" * 36,
        "github_pat": "ghp_" + "a" * 36,
        "slack_bot_token": "xoxb-123456789012-abcdefghijklmnop",
        "provider_api_key": "sk-ant-api03-" + "x" * 40,
        "openai_style_key": "sk-" + "A" * 40,
        "bearer_header": "Bearer abcdefghijklmnopqrstuvwxyz012345",
    }

    @pytest.mark.parametrize("name", sorted(SECRET_SHAPED))
    def test_the_model_refuses_secret_shaped_values(self, name):
        from app.models.credential import CredentialRegistry

        with pytest.raises(ValueError):
            CredentialRegistry(adp_credential_id=self.SECRET_SHAPED[name])

    @pytest.mark.parametrize("name", sorted(SECRET_SHAPED))
    def test_the_list_column_refuses_secret_shaped_values(self, name):
        """The same rule, on the column that holds several references."""
        from app.models.cloud_account import CloudAccount

        with pytest.raises(ValueError):
            CloudAccount(adp_credential_ids_json=json.dumps([self.SECRET_SHAPED[name]]))

    @pytest.mark.parametrize("name", sorted(SECRET_SHAPED))
    def test_the_api_boundary_refuses_secret_shaped_values(self, name):
        """A refusal at the request boundary is a 422 naming the field, not a 500."""
        import pydantic

        from app.schemas.account import RegisterAccountRequest

        with pytest.raises(pydantic.ValidationError):
            RegisterAccountRequest(
                name="prod",
                account_id="123456789012",
                role_arn="arn:aws:iam::123456789012:role/SuperplaneCrossAccount",
                external_id="ext-123",
                adp_credential_ids=[self.SECRET_SHAPED[name]],
            )

    def test_an_embedded_arn_is_reported_as_an_arn(self):
        """The message must name the actual problem, or the next person edits the wrong rule."""
        from app.models.credential import CredentialRegistry

        with pytest.raises(ValueError, match="must not be an ARN"):
            CredentialRegistry(
                adp_credential_id=self.SECRET_SHAPED["arn_after_a_label"]
            )

    def test_a_refusal_never_echoes_submitted_content_back(self):
        """No refusal may reflect any part of the submitted value back to the caller.

        These errors surface as 422 bodies and get logged. The ARN branch used to quote
        `candidate[:32]` so an operator could see which reference was refused, which is
        safe for an ARN but not for a value carrying *both* a secret and an ARN. Rule
        order alone did not make that safe: it only protects secrets the shape patterns
        recognize, and a bare 40-character AWS secret access key or a JWT has no
        distinctive prefix to match, so `"<secret> <role-arn>"` fell through to the ARN
        branch and echoed 32 characters of live credential. The branch now quotes only the
        matched ARN span, which is structural and contains no secret.

        Asserted on the discriminating substring `"must not be an ARN"` rather than on
        `"secret material"`: the ARN message also contains the phrase "secret material"
        ("a second reference to secret material living outside..."), so asserting on it
        cannot tell the two branches apart and would pass under a reversed, unsafe order.
        """
        from app.models.credential import validate_adp_credential_id

        recognized = "AKIAIOSFODNN7EXAMPLE"
        for value in (
            f"{recognized} arn:aws:iam::123456789012:role/x",
            f"arn:aws:iam::123456789012:role/x {recognized}",
        ):
            with pytest.raises(ValueError) as caught:
                validate_adp_credential_id(value)
            assert recognized not in str(caught.value), (
                "a refusal echoed the submitted secret back to the caller"
            )
            # The discriminating assertion: a recognized secret must take the
            # secret-shape branch, never the ARN branch that quotes a match.
            assert "must not be an ARN" not in str(caught.value), (
                "a recognized secret reached the quoting ARN branch; rule order broke"
            )

        # Secrets the shape patterns do NOT recognize still reach the ARN branch. That is
        # acceptable only because the branch no longer quotes the submitted value.
        for unrecognized in (
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # bare AWS secret access key
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.c2lnbmF0dXJl",  # JWT
        ):
            with pytest.raises(ValueError) as caught:
                validate_adp_credential_id(
                    f"{unrecognized} arn:aws:iam::123456789012:role/x"
                )
            assert unrecognized not in str(caught.value), (
                "the ARN branch echoed an unrecognized secret back to the caller"
            )
            for fragment_length in (8, 16, 24):
                assert unrecognized[:fragment_length] not in str(caught.value), (
                    "the ARN branch echoed a prefix of an unrecognized secret"
                )

        # The ARN branch still names what it refused, for a value that is only an ARN,
        # but quotes only the structural prefix — never the secret's name.
        with pytest.raises(ValueError, match="must not be an ARN"):
            validate_adp_credential_id(
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-AbCdEf"
            )
        with pytest.raises(ValueError) as caught:
            validate_adp_credential_id(
                "arn:aws:secretsmanager:us-east-1:123456789012:secret:prod-AbCdEf"
            )
        assert "prod-AbCdEf" not in str(caught.value), (
            "the ARN branch echoed the secret's name, not just the structural prefix"
        )

    def test_an_alphanumeric_prefix_does_not_hide_secret_material(self):
        """A single ASCII-alphanumeric character must not launder a credential.

        This is the third round of the same bug and the reason the boundary assertions
        ended up where they are. The original patterns used `\\b`, defeated by any *word*
        character (`_AKIA...`). Replacing `\\b` with `(?<![A-Za-z0-9])` narrowed the hole
        to *alphanumeric* characters but did not close it — `xAKIAIOSFODNN7EXAMPLE` was
        still stored, and `stored[1:]` recovers the key. A negative lookbehind cannot be
        the defence for a shape whose prefix is already distinctive, so those shapes carry
        no left assertion at all.

        `AKIA|ASIA` additionally drops its *trailing* assertion, because its body is a
        fixed `{16}` and so `AKIAIOSFODNN7EXAMPLEx` escaped as well; the open-ended bodies
        are greedy and absorb a trailing character regardless.
        """
        from app.models.credential import validate_adp_credential_id

        laundered = {
            "x_prefixed_access_key": "xAKIAIOSFODNN7EXAMPLE",
            "x_suffixed_access_key": "AKIAIOSFODNN7EXAMPLEx",
            "word_run_prefixed_access_key": "awsAKIAIOSFODNN7EXAMPLE",
            "digit_prefixed_access_key": "0AKIAIOSFODNN7EXAMPLE",
            "prefixed_session_key": "xASIAIOSFODNN7EXAMPLE",
            "prefixed_pat": "xghp_" + "a" * 36,
            "prefixed_slack_token": "xxoxb-123456789012-abcdefghijklmnop",
            # `sk-` keeps its left lookaround (`sk` is a common English word ending), so a
            # second unanchored tier requiring a longer unbroken run closes this affix
            # hole without refusing `risk-`/`task-`/`desk-` handles.
            "alnum_prefixed_provider_key": "xsk-ant-api03-" + "x" * 40,
            "word_run_prefixed_provider_key": "credsk-proj-" + "z" * 52,
            "alnum_prefixed_openai_style_key": "xsk-" + "A" * 40,
            "prefixed_bearer": "xBearer abcdefghijklmnopqrstuvwxyz012345",
            "prefixed_key_pair": (
                "xAKIAIOSFODNN7EXAMPLE:wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
            ),
        }
        for name, value in laundered.items():
            with pytest.raises(ValueError, match="secret material"):
                validate_adp_credential_id(value)
            assert name  # names the failing vector in the assertion output

    def test_a_confusable_character_cannot_smuggle_an_arn(self):
        """A value that normalizes back into an exact ARN must be refused.

        The patterns match ASCII `arn`, `:` and `[a-z0-9._-]`, so a fullwidth colon
        (U+FF1A) or a fullwidth `ａ` walked straight past them — and
        `unicodedata.normalize("NFKC", stored)` reproduced the original ARN *byte for
        byte*, so any downstream that normalizes (a JSON/YAML round-trip, a K8s label
        sanitizer, a non-Python client, an operator copying out of the UI) got a live
        address. Non-ASCII is refused outright rather than normalized-then-matched,
        because Cyrillic and combining-mark homoglyphs are not NFKC-equivalent to ASCII
        and would survive that approach.
        """
        import unicodedata

        from app.models.credential import validate_adp_credential_id

        real = "arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"
        confusables = {
            "fullwidth_colons": real.replace(":", "："),
            "fullwidth_a": "ａ" + real[1:],
            "fully_fullwidth": "".join(
                chr(ord(c) + 0xFEE0) if 0x21 <= ord(c) <= 0x7E else c for c in real
            ),
            "cyrillic_a_homoglyph": "а" + real[1:],
            "combining_mark": "á" + real[1:],
            "modifier_colon": real.replace(":", "∶"),
        }
        for name, value in confusables.items():
            with pytest.raises(ValueError, match="printable ASCII"):
                validate_adp_credential_id(value)
            assert name

        # Staleness guard: if any of these stopped normalizing back to the real ARN, the
        # case above would no longer be testing the property it claims to test.
        for name in ("fullwidth_colons", "fullwidth_a", "fully_fullwidth"):
            assert unicodedata.normalize("NFKC", confusables[name]) == real, (
                f"{name} no longer NFKC-normalizes to the ARN it is meant to smuggle"
            )

        # ASCII separators inside the service segment are structural, not confusable, so
        # they must be caught by the ARN rule itself rather than by the ASCII rule.
        for value in (
            "arn:aws:secrets_manager:us-east-1:1:secret:n",
            "arn:aws:secrets.manager:us-east-1:1:secret:n",
        ):
            with pytest.raises(ValueError, match="must not be an ARN"):
                validate_adp_credential_id(value)

    def test_an_interior_mutation_cannot_launder_an_arn_or_a_secret(self):
        """Laundering from the INSIDE, where no boundary assertion is involved.

        Three prior rounds hardened this rule against a leading or trailing affix, and
        their matrices are prefix/suffix only — so none of them could see that the same
        laundering works from the interior. The patterns match contiguous text, so a single
        space placed anywhere inside a real ARN left every one of them unmatched while a
        downstream whitespace strip restored the address byte for byte.

        Exhaustive single-character insertion at every position of a real secret ARN found
        32 such accepted values before this rule existed. `%3A` is the same evasion one
        encoding layer up: the ARN pattern matches a literal `:`, so percent-encoding the
        colons walked past it while anything that URL-decodes recovered the exact address.

        Each vector is asserted to be refused for its ACCURATE reason — an ARN as an ARN, a
        split access key as secret material — because the reason is what an operator acts
        on. That is why the rules test the recoverable forms instead of simply refusing
        whitespace outright, which would have reported every one of these as "whitespace".
        """
        import re

        from app.models.credential import validate_adp_credential_id

        real = "arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"

        # Interior-spaced ARNs, each recoverable by a plain whitespace strip.
        spaced = {
            "space_before_a_colon": (
                "arn:aws:secretsmanager:us-east-1 :123456789012:secret:nebius-AbCdEf"
            ),
            "space_after_the_scheme": (
                "arn: aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"
            ),
            "space_inside_the_service": (
                "arn:aws:secrets manager:us-east-1:123456789012:secret:nebius-AbCdEf"
            ),
        }
        for name, value in spaced.items():
            with pytest.raises(ValueError, match="must not be an ARN"):
                validate_adp_credential_id(value)
            assert re.sub(r"\s+", "", value) == real, (
                f"{name} no longer strips back to the ARN it is meant to smuggle"
            )

        # Percent-encoded colons: refused as an ARN, and the message must name the decoded
        # structure rather than the encoded string.
        encoded = real.replace(":", "%3A")
        with pytest.raises(ValueError, match="must not be an ARN") as caught:
            validate_adp_credential_id(encoded)
        assert re.sub(r"%3[Aa]", ":", encoded) == real, (
            "the percent-encoded vector no longer decodes to the ARN it smuggles"
        )
        assert "nebius-AbCdEf" not in str(caught.value), (
            "the refusal echoed the secret's name, not just the structural prefix"
        )

        # Secret shapes are launderable the same way: this strips back to a live key id.
        for value in ("AKIA IOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMP LE"):
            with pytest.raises(ValueError, match="secret material"):
                validate_adp_credential_id(value)

        # A value whose shape is NOT recognized (a bare secret access key, a JWT) has no
        # accurate ARN/secret reason available, so the residual whitespace rule catches it.
        for value in (
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY trailing",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig more",
        ):
            with pytest.raises(ValueError, match="must not contain whitespace"):
                validate_adp_credential_id(value)

    def test_an_escaped_arn_is_refused_in_every_encoding_a_consumer_decodes(self):
        """The escape class as a whole, not one sequence of it.

        The first version of this rule decoded only `%3A`, which is the identical mistake
        the three affix rounds made: it narrowed the hole rather than closing the class. A
        consumer that percent-decodes does not decode selectively, so encoding a different
        character of the same ARN walked straight past it — `%61rn:aws:...` is an encoded
        `a`, and `unquote` restores the exact address. HTML entities and JSON `\\uXXXX`
        escapes are the same evasion in the other two encodings a payload realistically
        passes through, and encodings stack.

        Every vector is asserted BOTH refused and still recoverable, so if a future change
        makes a vector stop decoding to the ARN, this test fails as stale rather than
        passing for the wrong reason.
        """
        import html
        import re
        import urllib.parse

        from app.models.credential import validate_adp_credential_id

        real = "arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"

        def _unescape_json(value: str) -> str:
            return re.sub(
                r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), value
            )

        deeply_encoded = real.replace(":", "%3A", 1)
        for _ in range(31):
            deeply_encoded = deeply_encoded.replace("%", "%25")

        def _decode_32_layers(value: str) -> str:
            for _ in range(32):
                value = urllib.parse.unquote(value)
            return value

        vectors = {
            # Percent-encoding a non-colon character: invisible to a `%3A`-only decode.
            "percent_encoded_letter": (
                "%61rn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf",
                urllib.parse.unquote,
            ),
            "percent_encoded_colons": (real.replace(":", "%3A"), urllib.parse.unquote),
            "html_entity_colons": (real.replace(":", "&#58;"), html.unescape),
            "html_named_entity": (
                real.replace(":", "&colon;"),
                html.unescape,
            ),
            "json_unicode_colons": (real.replace(":", "\\u003a"), _unescape_json),
            # Two layers: `&#37;` unescapes to `%`, leaving `%3A` for the percent decode.
            "stacked_entity_then_percent": (
                real.replace(":", "&#37;3A"),
                lambda v: urllib.parse.unquote(html.unescape(v)),
            ),
            # Multi-layer percent encoding. Decoding a FIXED NUMBER of layers rather than to
            # a fixed point left these accepted: one `unquote` pass turns `%253A` into
            # `%3A`, which still hides the colon. Any consumer that decodes twice recovers
            # the exact address, so depth must not be what decides the outcome.
            "double_encoded_colons": (
                real.replace(":", "%253A"),
                lambda v: urllib.parse.unquote(urllib.parse.unquote(v)),
            ),
            "triple_encoded_colons": (
                real.replace(":", "%25253A"),
                lambda v: urllib.parse.unquote(
                    urllib.parse.unquote(urllib.parse.unquote(v))
                ),
            ),
            # The production column admits this 32-layer value comfortably. This guards
            # against calling an arbitrary small round limit a fixed-point decoder.
            "deeply_encoded_colon": (deeply_encoded, _decode_32_layers),
        }

        for name, (value, decode) in vectors.items():
            assert decode(value) == real, (
                f"{name} no longer decodes to the ARN it is meant to smuggle — "
                "the vector is stale, not the rule"
            )
            with pytest.raises(ValueError, match="must not be an ARN") as caught:
                validate_adp_credential_id(value)
            # The decoded structure is the accurate reason, and the message must still not
            # reflect the secret's own name back into a 422 body or the logs.
            assert "nebius-AbCdEf" not in str(caught.value), (
                f"the refusal for {name} echoed the secret's name"
            )

        # Reached through the list column too, not only the scalar validator.
        from app.models.cloud_account import CloudAccount

        with pytest.raises(ValueError, match="must not be an ARN"):
            CloudAccount(
                adp_credential_ids_json=json.dumps(
                    ["adp-cred-01HQ8V3XK2WERTY", real.replace(":", "&#58;")]
                )
            )

    def test_a_case_variant_access_key_is_not_treated_as_secret_material(self):
        """The key-id rule is case-sensitive on purpose, and this pins the reasoning.

        Matching it case-insensitively was tried and reverted. An AWS access key id is
        uppercase and case-sensitive, and no decode step in this validator recovers the
        uppercase form, so a lowercase lookalike is not a usable key — there is nothing to
        protect. It is not free either: case-insensitive matching refused a legitimate
        44-character handle over a 300k-handle corpus, because an incidental mixed-case
        `akIa`+16 run is reachable by chance in a way an uppercase one is much less so.

        The uppercase form stays refused, which is the case that matters.
        """
        from app.models.credential import validate_adp_credential_id

        assert (
            validate_adp_credential_id("u3pXKcVTL63jCtJRfakIaTObUB2FgWClh5Vftqnizabt")
            == "u3pXKcVTL63jCtJRfakIaTObUB2FgWClh5Vftqnizabt"
        )
        assert validate_adp_credential_id("akiaiosfodnn7example") == (
            "akiaiosfodnn7example"
        )
        with pytest.raises(ValueError, match="secret material"):
            validate_adp_credential_id("AKIAIOSFODNN7EXAMPLE")

    def test_no_refusal_branch_echoes_submitted_secret_material(self):
        """Whichever rule fires, the message must not reflect the submitted value back.

        These messages reach a 422 body and the logs. A value carrying both a secret and
        an ARN previously took the ARN branch and echoed `candidate[:32]`; the interior
        rules added another way to reach that branch, so the property is re-checked for
        every vector rather than assumed to still hold.
        """
        from app.models.credential import validate_adp_credential_id

        secrets_that_must_not_appear = (
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "eyJhbGciOiJIUzI1NiJ9",
            "IOSFODNN7EXAMPLE",
            "nebius-AbCdEf",
        )
        vectors = (
            "arn:aws:secretsmanager:us-east-1 :123456789012:secret:nebius-AbCdEf",
            "arn%3Aaws%3Asecretsmanager%3Aus-east-1%3A1%3Asecret%3Anebius-AbCdEf",
            "AKIA IOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY extra",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig more",
            "AKIAIOSFODNN7EXAMPLE arn:aws:iam::123456789012:role/x",
        )
        for value in vectors:
            with pytest.raises(ValueError) as caught:
                validate_adp_credential_id(value)
            message = str(caught.value)
            for fragment in secrets_that_must_not_appear:
                assert fragment not in message, (
                    f"the refusal for {value[:24]!r}... echoed {fragment!r}"
                )

    def test_an_invisible_character_is_reported_as_such(self):
        from app.models.credential import CredentialRegistry

        with pytest.raises(ValueError, match="invisible or control characters"):
            CredentialRegistry(adp_credential_id="adp-cred-01HQ​V3XK2WERTY")

    # Opaque handles that must keep working. A guard that refused these would break
    # credential registration outright, which is a worse outcome than the leak it prevents.
    LEGITIMATE = {
        "vault_uuid": "3f2a1b4c-5d6e-7f80-9a1b-2c3d4e5f6071",
        "adp_prefixed_handle": "adp-cred-01HQ8V3XK2WERTY",
        "short_handle": "cred-42",
        "underscored_handle": "nebius_prod_key_ref",
        "dotted_handle": "cred.prod.nebius.1",
        "opaque_36_chars": "a" * 36,
        # Handles that merely *contain* a secret-ish prefix as a word segment. Matching
        # secret shapes by prefix alone would refuse these, and because the rule runs in a
        # `@validates` hook that is an unregisterable credential, not a cosmetic 422 — a
        # strictly worse outcome than the leak the rule prevents. The distinguishing signal
        # is a long unbroken high-entropy run, which a word slug does not have.
        "sk_prefixed_word_slug": "sk-prod-nebius-credential-ref1",
        "sk_infixed_word_slug": "nebius-sk-prod-credential-ref-1",
        # Substrings of the rule's own vocabulary must not be enough on their own.
        "handle_containing_arn": "my_arn_reference",
        "handle_named_for_a_role_arn": "role_arn_cred",
        "bare_word_arn": "arn",
        # `sk-` is the one secret shape that KEEPS a left lookaround, because `sk` is a
        # common English word ending. These are the handles that would be refused without
        # it, so they pin why that one boundary is load-bearing while the others are not.
        "risk_prefixed_handle": "risk-01HQ8V3XK2WERTYUIOPASDFGH",
        "task_prefixed_handle": "task-01HQ8V3XK2WERTYUIOPASDFGH",
        "disk_prefixed_handle": "disk-encryption-key-01HQ8V3XK2WERTY",
        # Lowercase or mixed-case vocabulary collisions with the now-unanchored shapes.
        "akia_worded_handle": "akia-prod-credential-reference",
        "bearer_worded_handle": "bearer_prod_credential",
        "ghp_worded_handle": "ghp-prod-cred",
        # Path- and colon-separated handle styles, which the widened ARN segment class
        # must not mistake for an ARN structure.
        "path_style_handle": "adp/nebius/prod",
        "kv_path_handle": "kv/data/nebius/prod",
        "colon_scoped_handle": "cred:v1:nebius:prod",
    }

    @pytest.mark.parametrize("name", sorted(LEGITIMATE))
    def test_legitimate_references_are_still_accepted(self, name):
        from app.models.credential import CredentialRegistry

        value = self.LEGITIMATE[name]
        assert CredentialRegistry(adp_credential_id=value).adp_credential_id == value


class TestTheMirroredSecretRulesAgreeWithTheContract:
    """The model's ARN and secret patterns are a mirror, and a mirror can drift.

    `app/models/credential.py` copies `superplane_contracts.secrets` as plain values rather
    than importing it, for the reason `app/services/provisioning.py` documents: the API
    image is built with a context pinned to `src/superplane-api`, so the contracts package
    is genuinely not importable at API runtime, and `alembic/env.py` imports `app.models`,
    which would make it a migration-runner requirement too.

    That arrangement is only safe if drift fails a test. CI puts both packages on
    `sys.path`, so these tests compare the two — but on **behaviour**, not on pattern text.
    Comparing the strings would be the easier test and the wrong one: the local copies
    deliberately differ (see below), so a string equality would have to be updated to
    whatever the code says, which is not a check. What must hold is the *direction* of the
    difference — the local rule may refuse more than the contract, never less.
    """

    # Values the contract's rules catch. The local copy must catch all of them.
    CONTRACT_CATCHES = (
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:k-AbCdEf",
        "arn:aws-cn:kms:cn-north-1:123456789012:key/abc",
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAIOSFODNN7EXAMPLE",
        "ghp_" + "a" * 36,
        "xoxb-123456789012-abcdefghijkl",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Bearer abcdefghijklmnopqrstuvwxyz012345",
        "sk-" + "A" * 40,
        # A zero-width character is not whitespace, so `.strip()` leaves it in place — but
        # it *is* a non-word character, so the contract's `\b` still matches. This is in the
        # shared list, not the divergence list: what the prefix check in the first
        # implementation missed, the contract's rule already caught.
        "​arn:aws:secretsmanager:us-east-1:123456789012:secret:k",
    )

    @pytest.mark.parametrize("value", CONTRACT_CATCHES)
    def test_the_model_refuses_everything_the_contract_calls_secret(self, value):
        """The mirror may be stricter than the contract, never weaker."""
        from superplane_contracts.secrets import value_is_secret_shaped

        from app.models.credential import validate_adp_credential_id

        assert value_is_secret_shaped(value), (
            "test corpus is stale: the contract no longer calls this secret-shaped"
        )
        with pytest.raises(ValueError):
            validate_adp_credential_id(value)

    LOCAL_IS_STRONGER = {
        "word_prefixed_arn": "_arn:aws:secretsmanager:us-east-1:123456789012:secret:k",
        "name_prefixed_arn": (
            "secret_arn:aws:secretsmanager:us-east-1:123456789012:secret:k"
        ),
        "word_prefixed_access_key": "_AKIAIOSFODNN7EXAMPLE",
        "word_suffixed_access_key": "AKIAIOSFODNN7EXAMPLE_",
        "word_prefixed_pat": "_ghp_" + "a" * 36,
        "hyphenated_provider_key": "sk-ant-api03-" + "x" * 40,
    }

    @pytest.mark.parametrize("name", sorted(LOCAL_IS_STRONGER))
    def test_contract_and_model_reject_recoverable_secret_corpus(self, name):
        from superplane_contracts.secrets import value_is_secret_shaped

        from app.models.credential import validate_adp_credential_id

        value = self.LOCAL_IS_STRONGER[name]

        assert value_is_secret_shaped(value), (
            f"contract no longer rejects recoverable material: {name!r}"
        )
        with pytest.raises(ValueError):
            validate_adp_credential_id(value)

    CONTRACT_ARNS = (
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf",
        "arn:aws-cn:kms:cn-north-1:123456789012:key/abcd",
        "arn:aws-us-gov:ssm:us-gov-west-1:123456789012:parameter/nebius",
        "arn:aws:iam::123456789012:role/nebius-cross-account",
        "ARN:AWS:SECRETSMANAGER:US-EAST-1:123456789012:SECRET:NEBIUS",
    )

    LOCAL_ARNS_ONLY = {
        "word_prefixed": "_arn:aws:secretsmanager:us-east-1:123456789012:secret:k",
        "old_column_name_prefixed": (
            "secret_arn:aws:secretsmanager:us-east-1:123456789012:secret:k"
        ),
        "underscored_service": "arn:aws:secrets_manager:us-east-1:1:secret:nebius",
        "dotted_service": "arn:aws:secrets.manager:us-east-1:1:secret:nebius",
    }

    @pytest.mark.parametrize("value", CONTRACT_ARNS)
    def test_every_arn_the_contract_catches_is_caught_locally(self, value):
        """The mirror may be stronger than the contract, never weaker.

        Asserted behaviourally rather than by comparing pattern strings. A string equality
        has to be edited to whatever the code currently says, so it cannot fail usefully —
        it failed on this story's own deliberate widening of the segment class and the only
        available response was to rewrite the expected string. What matters is the
        *direction* of the difference, which is what this asserts.
        """
        from superplane_contracts.secrets import looks_like_arn

        from app.models.credential import validate_adp_credential_id

        assert looks_like_arn(value), (
            f"{value!r} is no longer caught by the contract, so this case no longer tests "
            "that the mirror covers the contract's structure"
        )
        with pytest.raises(ValueError, match="must not be an ARN"):
            validate_adp_credential_id(value)

    @pytest.mark.parametrize("name", sorted(LOCAL_ARNS_ONLY))
    def test_contract_and_model_reject_embedded_arns(self, name):
        from superplane_contracts.secrets import looks_like_arn

        from app.models.credential import validate_adp_credential_id

        value = self.LOCAL_ARNS_ONLY[name]

        assert looks_like_arn(value), (
            f"contract no longer rejects recoverable material: {name!r}"
        )
        with pytest.raises(ValueError, match="must not be an ARN"):
            validate_adp_credential_id(value)

    def test_contract_rejects_prefixed_recoverable_material(self):
        from superplane_contracts.secrets import looks_like_arn, value_is_secret_shaped

        assert looks_like_arn("_arn:aws:secretsmanager:us-east-1:123456789012:secret:k")
        assert value_is_secret_shaped("xAKIAIOSFODNN7EXAMPLE")


class TestProviderConnectionPersistence:
    """The two R7 records, and the constraint that is not merely application code.

    Issue #5053 (U7b). `superplane_contracts.connections` decides whether an
    operation is allowed, but every decision function takes the ownership record
    and the binding as *arguments* — it reads no storage. These tables are what
    supplies those arguments, so a property the contract guarantees in Python is
    only real end-to-end if storage cannot produce a state the contract would
    have refused to construct.

    The binding's "exactly one workspace" is the case that matters. The contract
    enforces it by *being* a two-scalar frozen dataclass, which stops nobody from
    writing two rows: `authorize_use` would then be called with whichever row the
    query happened to return first, and row order would decide a tenant-isolation
    question. So the rule is a database constraint, and these tests exercise it
    through a real flush rather than through the validator.
    """

    OPAQUE = "adp-cred-01HQ8V3XK2WERTY"
    REPLACEMENT = "adp-cred-01HQ8V3XK2ZZZZ"
    ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:nebius-AbCdEf"

    async def _seed_org_and_workspace(self):
        """An org + workspace for the foreign keys to point at."""
        import uuid

        from app.models.organization import Organization
        from app.models.workspace import Workspace
        from tests.conftest import async_session_test

        org_id, workspace_id = uuid.uuid4(), uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                Organization(
                    id=org_id, name=f"conn-org-{org_id.hex[:8]}", billing_plan="free"
                )
            )
            session.add(
                Workspace(
                    id=workspace_id,
                    org_id=org_id,
                    name="ws",
                    isolation_mode="shared",
                    status="active",
                )
            )
            await session.commit()
        return org_id, workspace_id

    async def _seed_connection(self, org_id, credential_id=None):
        import uuid

        from app.models.provider_connection import ProviderConnection
        from tests.conftest import async_session_test

        connection_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                ProviderConnection(
                    id=connection_id,
                    org_id=org_id,
                    provider="nebius",
                    adp_credential_id=credential_id or self.OPAQUE,
                    credential_service="nebius",
                    credential_label="nebius-prod",
                    owner_principal="user-owner",
                )
            )
            await session.commit()
        return connection_id

    def _binding(self, connection_id, workspace_id, credential_id=None):
        import uuid

        from app.models.provider_connection import ProviderConnectionBinding

        return ProviderConnectionBinding(
            id=uuid.uuid4(),
            connection_id=connection_id,
            adp_credential_id=credential_id or self.OPAQUE,
            workspace_id=workspace_id,
            bound_by="user-owner",
        )

    # --- the constraint ---

    async def test_a_second_binding_for_the_same_credential_is_rejected(self):
        """Two workspaces cannot both hold a binding for one connection.

        Asserted at the database, not at the validator, because the validator is
        what a backfill or a fixture bypasses. The second workspace here is a
        *different* workspace in the same org — the exact shape of the bug this
        constraint exists to stop, where a credential delegated to one workspace
        silently becomes usable by another.
        """
        import uuid

        from sqlalchemy.exc import IntegrityError

        from app.models.workspace import Workspace
        from tests.conftest import async_session_test

        org_id, workspace_id = await self._seed_org_and_workspace()
        connection_id = await self._seed_connection(org_id)

        other_workspace = uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=other_workspace,
                    org_id=org_id,
                    name="ws-two",
                    isolation_mode="shared",
                    status="active",
                )
            )
            session.add(self._binding(connection_id, workspace_id))
            await session.commit()

        async with async_session_test() as session:
            session.add(self._binding(connection_id, other_workspace))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_one_binding_is_accepted_and_readable(self):
        """The constraint refuses a second row without refusing the first.

        Paired with the test above deliberately: a constraint that rejected
        everything would make that test pass while making the feature useless.
        """
        from sqlalchemy import select

        from app.models.provider_connection import ProviderConnectionBinding
        from tests.conftest import async_session_test

        org_id, workspace_id = await self._seed_org_and_workspace()
        connection_id = await self._seed_connection(org_id)
        async with async_session_test() as session:
            session.add(self._binding(connection_id, workspace_id))
            await session.commit()

        async with async_session_test() as session:
            rows = (
                (
                    await session.execute(
                        select(ProviderConnectionBinding).where(
                            ProviderConnectionBinding.connection_id == connection_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].workspace_id == workspace_id
        assert rows[0].adp_credential_id == self.OPAQUE

    async def test_the_same_credential_cannot_be_registered_twice_in_one_org(self):
        """Two connection ids for one credential would make rotation incoherent.

        Rotating through one row leaves the other pointing at a superseded
        reference while still reporting itself active, so the duplicate is
        refused at the database rather than reconciled later.
        """
        import uuid

        from sqlalchemy.exc import IntegrityError

        from app.models.provider_connection import ProviderConnection
        from tests.conftest import async_session_test

        org_id, _ = await self._seed_org_and_workspace()
        await self._seed_connection(org_id)

        async with async_session_test() as session:
            session.add(
                ProviderConnection(
                    id=uuid.uuid4(),
                    org_id=org_id,
                    provider="nebius",
                    adp_credential_id=self.OPAQUE,
                    credential_service="nebius",
                    credential_label="nebius-prod-again",
                    owner_principal="user-owner",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()

    # --- the reference columns refuse secret material ---

    def test_the_connection_refuses_an_arn(self):
        """The reference column reuses U13b's validator rather than a weaker copy."""
        from app.models.provider_connection import ProviderConnection

        with pytest.raises(ValueError, match="must not be an ARN"):
            ProviderConnection(adp_credential_id=self.ARN)

    def test_the_binding_refuses_an_arn(self):
        """The denormalized copy is guarded too, or the rule holds on one column only."""
        from app.models.provider_connection import ProviderConnectionBinding

        with pytest.raises(ValueError, match="must not be an ARN"):
            ProviderConnectionBinding(adp_credential_id=self.ARN)

    def test_both_columns_refuse_every_secret_shape_the_registry_refuses(self):
        """Whatever `credential_registry` refuses, these two columns refuse.

        Written as one test over the shared corpus rather than a second copy of it:
        the corpus was hardened against specific measured bypasses, and a divergence
        between the tables would be the gap. Any value the older column rejects must
        be rejected here, and the assertion names the value's label — not its
        content — so a failure is diagnosable without printing a credential.
        """
        from app.models.credential import CredentialRegistry
        from app.models.provider_connection import (
            ProviderConnection,
            ProviderConnectionBinding,
        )

        corpus = TestTheReferenceRuleCannotBeSteppedAround.SECRET_SHAPED
        assert corpus, (
            "the shared secret-shape corpus is empty; this test proves nothing"
        )
        for label, value in sorted(corpus.items()):
            try:
                CredentialRegistry(adp_credential_id=value)
            except ValueError:
                pass
            else:  # pragma: no cover - the corpus is a refusal corpus
                raise AssertionError(
                    f"{label!r} is no longer refused by credential_registry; "
                    "the corpus changed meaning"
                )
            with pytest.raises(ValueError):
                ProviderConnection(adp_credential_id=value)
            with pytest.raises(ValueError):
                ProviderConnectionBinding(adp_credential_id=value)

    # --- lifecycle and reporting columns ---

    def test_the_status_vocabulary_matches_the_contract(self):
        """The stored strings are the contract's enum values, asserted not assumed.

        The column is text rather than a database enum, so nothing structural ties
        it to `ConnectionStatus`. This test is that tie: an upstream rename fails
        here instead of silently detaching storage from the decision vocabulary.
        """
        from superplane_contracts.connections import ConnectionStatus

        from app.models.provider_connection import CONNECTION_STATUSES

        assert set(CONNECTION_STATUSES) == {str(s) for s in ConnectionStatus}

    def test_an_unknown_status_is_refused(self):
        """An unrecognized status reads as "not disabled" downstream — permissively."""
        from app.models.provider_connection import ProviderConnection

        with pytest.raises(ValueError, match="status must be one of"):
            ProviderConnection(status="quarantined")

    async def test_unmeasured_capacity_is_distinct_from_measured_zero(self):
        """R7 acceptance 3 survives a round trip through storage.

        "We did not look" and "we looked and there is nothing free" are different
        operational facts. A non-nullable column defaulting to 0 would erase the
        distinction at the storage layer, after the contract went to some trouble
        to preserve it, so this asserts `None` comes back as `None` and `0` comes
        back as `0` — and as an `int`, not as a falsy string.
        """
        import uuid

        from sqlalchemy import select

        from app.models.provider_connection import ProviderConnection
        from tests.conftest import async_session_test

        org_id, _ = await self._seed_org_and_workspace()
        unmeasured, measured_zero = uuid.uuid4(), uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                ProviderConnection(
                    id=unmeasured,
                    org_id=org_id,
                    provider="nebius",
                    adp_credential_id=self.OPAQUE,
                    credential_service="nebius",
                    credential_label="unmeasured",
                    owner_principal="user-owner",
                    credential_valid=True,
                    permissions_sufficient=True,
                    quota_available=True,
                    observed_capacity=None,
                )
            )
            session.add(
                ProviderConnection(
                    id=measured_zero,
                    org_id=org_id,
                    provider="nebius",
                    adp_credential_id=self.REPLACEMENT,
                    credential_service="nebius",
                    credential_label="measured-zero",
                    owner_principal="user-owner",
                    credential_valid=True,
                    permissions_sufficient=True,
                    quota_available=True,
                    observed_capacity=0,
                )
            )
            await session.commit()

        async with async_session_test() as session:
            rows = {
                row.id: row
                for row in (
                    (
                        await session.execute(
                            select(ProviderConnection).where(
                                ProviderConnection.org_id == org_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
        assert rows[unmeasured].observed_capacity is None
        assert rows[measured_zero].observed_capacity == 0
        assert isinstance(rows[measured_zero].observed_capacity, int)

    async def test_a_new_connection_starts_pending_and_admits_nothing(self):
        """The default is the status that admits no work, not the usable one."""
        from app.models.provider_connection import STATUS_PENDING, ProviderConnection
        from tests.conftest import async_session_test

        org_id, _ = await self._seed_org_and_workspace()
        connection_id = await self._seed_connection(org_id)
        async with async_session_test() as session:
            row = await session.get(ProviderConnection, connection_id)
            assert row.status == STATUS_PENDING
            assert row.credential_valid is None
            assert row.limitation == ""
