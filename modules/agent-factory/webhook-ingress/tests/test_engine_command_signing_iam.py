"""The engine-command signing key is readable by exactly two principals (issue #4539).

The whole of #4539 rests on one claim: the tick can tell a delivered command tuple from
an authored one, because only the webhook Lambda holds the key that signs it. That claim
is not a property of the Python — the Python is correct either way — it is a property of
these three policies. If the agent worker cohort can read this secret, a worker can mint
an envelope naming any tenant, any commenter and any repository, and attach a signature
that verifies.

So these tests assert the boundary rather than the mechanism, and they assert it against
policy JSON rendered from the real expressions (see `render_engine_command_policies.py`)
rather than against the file text. Fixture ARNs stand in for provider outputs; this is
not a live IAM acceptance test, and the activation checklist's "prove the worker cannot
read the key" step is still an `aws sts assume-role` + `get-secret-value` against the
deployed environment. What these catch is the regression that lands in a diff: a
principal added to the reader list, a condition dropped, a write action appearing on the
verifier, the deny statement deleted.

**Comments are stripped before rendering**, deliberately — the assertions must not be
satisfiable by prose. Reworded reasoning cannot make these pass; a widened grant cannot
keep them passing.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ACCOUNT = "879318057152"
SIGNER = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-webhook-lambda-role"
VERIFIER = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-gateway-orchestration-tick-role"
SCALEDJOB_WORKER = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-agent-scaledjob-role"
AUTHORITY_WORKER = f"arn:aws:iam::{ACCOUNT}:role/adp-dev-agent-authority-worker-role"
WORKER_COHORT = {SCALEDJOB_WORKER, AUTHORITY_WORKER}

SECRET_ARN = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:adp/dev/webhook-ingress/engine-command-signing-key-AbCdEf"
KMS_ARN = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/engine-command-signing-key"

INFRA = Path(__file__).resolve().parents[1] / "infra"
SOURCE = INFRA / "engine-command-signing.tf"


@pytest.fixture(scope="module")
def policies():
    if shutil.which("terraform") is None:
        pytest.skip("Terraform is needed to render policy expressions")
    rendered = subprocess.check_output(
        [sys.executable, str(Path(__file__).with_name("render_engine_command_policies.py"))],
        text=True,
    )
    return json.loads(rendered)


def _principals(statement) -> set[str]:
    principal = statement["Principal"]["AWS"]
    return {principal} if isinstance(principal, str) else set(principal)


def _by_sid(policy) -> dict[str, dict]:
    statements = policy["Statement"]
    by_sid = {s["Sid"]: s for s in statements}
    # Duplicate Sids would make every lookup below silently test only the last one.
    assert len(by_sid) == len(statements), "duplicate Sid in policy"
    return by_sid


class TestOnlyTheSignerAndTheVerifierCanDecryptTheKey:
    def test_the_reader_list_is_exactly_two_roles(self, policies):
        """Not "does not include the worker" — an exact set.

        A negative assertion passes when a third role is added, and the third role is
        how this leaks: the ARC runner, a debug role, a future supervisor that "just
        needs to check". Every addition to this list has to be argued for in a diff that
        also edits this test.
        """
        readers = _principals(_by_sid(policies["key"])["AllowSignerAndVerifierDecrypt"])

        assert readers == {SIGNER, VERIFIER}

    def test_the_decrypt_grant_is_bound_to_this_secret_via_secrets_manager(self, policies):
        """The encryption-context condition is what makes the grant narrow.

        Without `kms:EncryptionContext:SecretARN` this statement says "these roles may
        decrypt anything encrypted with this key", which is a materially different and
        much larger grant the moment a second secret is put on the key.
        """
        condition = _by_sid(policies["key"])["AllowSignerAndVerifierDecrypt"]["Condition"]

        assert condition["StringEquals"]["kms:ViaService"] == "secretsmanager.us-east-1.amazonaws.com"
        secret_context = condition["StringEquals"]["kms:EncryptionContext:SecretARN"]
        assert secret_context.startswith(f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:adp/dev/webhook-ingress/engine-command-signing-key")
        # Secrets Manager appends a random 6-char suffix to the name, so a wildcard tail
        # is unavoidable — but it must be a tail on the FULL name, not a path wildcard.
        assert "adp/*" not in secret_context
        assert "/dev/*" not in secret_context

    def test_no_statement_grants_the_worker_cohort_anything(self, policies):
        """Across all three policies, and for Allow of any shape."""
        for name, policy in policies.items():
            for statement in policy["Statement"]:
                if statement["Effect"] != "Allow" or "Principal" not in statement:
                    continue
                assert not (_principals(statement) & WORKER_COHORT), f"{name}/{statement['Sid']} grants a worker role"

    def test_the_key_policy_names_no_wildcard_principal(self, policies):
        """`Principal: "*"` on a key policy is the failure that makes everything else moot.

        With `secretsmanager:GetSecretValue` on `secret:adp/*` already held by the worker
        cohort (`scaledjob-iam.tf`'s `SecretsManagerOps`), a wildcard principal here is
        the entire forgery path in one character.
        """
        for statement in policies["key"]["Statement"]:
            if statement["Effect"] != "Allow":
                continue
            assert "*" not in _principals(statement)


class TestTheWorkerCohortIsDeniedOnTheKeyItself:
    """A resource-side Deny, so a new identity policy cannot grant its way in.

    The authority boundary's `DenyAllSecrets` is an IAM control on a role, and it is only
    attached while `agent_authority_enabled` is true. This one is attached to the key.
    """

    def test_both_worker_roles_are_denied(self, policies):
        deny = _by_sid(policies["key"])["DenyAgentWorkerCohort"]

        assert deny["Effect"] == "Deny"
        assert _principals(deny) == WORKER_COHORT

    def test_the_denial_is_unconditional_and_covers_every_kms_action(self, policies):
        """A condition on a Deny is a gap: it stops applying the moment the condition is
        not met, which is exactly the request an attacker constructs."""
        deny = _by_sid(policies["key"])["DenyAgentWorkerCohort"]

        assert deny["Action"] == "kms:*"
        assert deny["Resource"] == "*"
        assert "Condition" not in deny

    def test_the_pre_authority_worker_is_covered_even_though_it_predates_the_boundary(self, policies):
        """`agent_scaledjob` is the role that actually runs today.

        It holds `secretsmanager:GetSecretValue` on `secret:adp/*`, so it is the concrete
        principal this key is defended against — not the authority worker, which is
        behind a flag and already carries `DenyAllSecrets`.
        """
        assert SCALEDJOB_WORKER in _principals(_by_sid(policies["key"])["DenyAgentWorkerCohort"])


class TestTheVerifierCanOnlyRead:
    """A verifier that could write the keyring could install a key it also holds.

    At that point the signature proves nothing: the component checking authenticity would
    be able to choose the secret the authenticity is measured against. This is why the
    grant is asymmetric even though both sides need the same key material.
    """

    def test_the_verifier_gets_no_write_action_of_any_kind(self, policies):
        actions = {a for s in policies["verifier"]["Statement"] for a in s["Action"]}

        assert actions == {
            "secretsmanager:GetSecretValue",
            "secretsmanager:DescribeSecret",
            "kms:Decrypt",
            "kms:DescribeKey",
        }

    def test_the_signer_gets_no_write_action_either(self, policies):
        """Seeding and rotation are operator actions performed out of band.

        The signer needs to read the active key; it never needs to create one. A signer
        that could rotate the keyring could also roll it forward past the verifier's
        overlap window and silently break every pending command.
        """
        actions = {a for s in policies["signer"]["Statement"] for a in s["Action"]}

        assert not any(
            a.startswith(("secretsmanager:Put", "secretsmanager:Update", "secretsmanager:Create", "secretsmanager:Rotate", "secretsmanager:Delete", "kms:Encrypt", "kms:GenerateDataKey"))
            for a in actions
        )

    @pytest.mark.parametrize("role", ["signer", "verifier"])
    def test_each_grant_names_one_secret_and_one_key(self, policies, role):
        """No wildcards, no `secret:adp/*`, no `key/*`."""
        resources = {s["Resource"] for s in policies[role]["Statement"]}

        assert resources == {SECRET_ARN, KMS_ARN}

    @pytest.mark.parametrize("role", ["signer", "verifier"])
    def test_the_identity_side_decrypt_is_conditioned_the_same_way(self, policies, role):
        """The identity policy repeats the key policy's condition rather than relying on it.

        Either alone is sufficient for the current shape, which is the argument for having
        both: a change to one is then not enough to widen the grant.
        """
        kms_statement = next(s for s in policies[role]["Statement"] if s["Resource"] == KMS_ARN)

        assert kms_statement["Condition"]["StringEquals"] == {
            "kms:ViaService": "secretsmanager.us-east-1.amazonaws.com",
            "kms:EncryptionContext:SecretARN": SECRET_ARN,
        }


class TestTheVerifierGrantFailsClosedWhenUnwired:
    """The verifier lives in the gateway's Terraform state, so its ARN is a variable.

    An unset variable must not become a guessed ARN or a wildcard. It creates no grant,
    which means the tick cannot read the key, which means every command is quarantined
    with `no_verification_key` — commands stop working, and nothing is forgeable.
    """

    def test_an_empty_arn_creates_no_verifier_policy(self):
        source = SOURCE.read_text(encoding="utf-8")

        assert 'count = var.engine_command_verifier_role_arn != "" ? 1 : 0' in source

    def test_an_empty_arn_is_dropped_from_the_key_policy_principals(self):
        """`compact()` is what keeps the empty string out of the principal list.

        An empty string in a KMS principal list is not ignored — the apply fails, and the
        tempting fix is a wildcard. `compact()` makes the unwired case simply produce a
        one-reader key.
        """
        source = SOURCE.read_text(encoding="utf-8")

        assert "compact([" in source
        assert "var.engine_command_verifier_role_arn," in source

    def test_the_variable_rejects_a_user_or_session_arn(self):
        """Shape validation, because Terraform cannot confirm a role in another state."""
        variables = (INFRA / "variables.tf").read_text(encoding="utf-8")
        block = variables[variables.index('variable "engine_command_verifier_role_arn"') :]
        block = block[: block.index("\nvariable ") if "\nvariable " in block else len(block)]

        assert "arn:aws:iam::[0-9]{12}:role/" in block


class TestTheKeyValueNeverEntersTerraform:
    """Terraform provisions the container and the grants; it must never learn the key.

    State is not a secret store: it is read by CI, cached in S3, and printed by `terraform
    show`. A key in state is a key in every place state goes.
    """

    def test_the_seeded_version_is_a_placeholder_both_implementations_refuse(self):
        source = SOURCE.read_text(encoding="utf-8")

        assert "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND" in source

    def test_the_placeholder_is_the_exact_string_the_code_refuses(self):
        """#4128: a signature computed under a placeholder that ships in this repo REPORTS
        SUCCESS, which is strictly worse than no verification at all. The guard only holds
        if the string here and the string in `_PLACEHOLDER_KEYS` are the same string.
        """
        source = SOURCE.read_text(encoding="utf-8")
        placeholder = "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"
        verifier = Path(__file__).resolve().parents[3] / "gateway" / "src" / "orchestration" / "command_attribution.py"

        assert placeholder in source
        assert verifier.is_file(), f"missing {verifier} — path arithmetic is stale, fix it rather than skipping"
        assert placeholder in verifier.read_text(encoding="utf-8"), (
            "the Terraform placeholder is not in the verifier's _PLACEHOLDER_KEYS — an unseeded "
            "environment would verify signatures against a value published in git"
        )

    def test_subsequent_applies_ignore_the_real_value(self):
        """Without `ignore_changes`, the first apply after seeding shows the operator's key
        as a plan diff and then overwrites it with the placeholder."""
        source = SOURCE.read_text(encoding="utf-8")

        assert "ignore_changes = [secret_string]" in source

    def test_nothing_in_the_file_outputs_a_secret_value(self):
        """Outputs and SSM parameters carry ARNs only."""
        source = SOURCE.read_text(encoding="utf-8")

        assert "aws_secretsmanager_secret_version.engine_command_signing_key.secret_string" not in source
        # `data "aws_secretsmanager_secret_version"` would read the live value INTO state,
        # which is the same leak by a different route.
        assert 'data "aws_secretsmanager_secret_version"' not in source

    def test_the_secret_has_a_recovery_window(self):
        """A deleted keyring makes every pending command permanently unverifiable.

        Fails closed, but irrecoverably — the window is the difference between restoring
        it and re-seeding while commands are refused.
        """
        source = SOURCE.read_text(encoding="utf-8")

        assert "recovery_window_in_days = 7" in source


class TestTheVerifierIsActuallyWiredOnTheGatewaySide:
    """The grants above are useless if the tick never learns the secret's ARN.

    Everything in this file so far proves the webhook-ingress side: the secret exists,
    exactly two principals can decrypt it, the worker cohort is denied, the value stays
    out of state. None of that establishes that the verifier can find the key.

    The tick reads `ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN` from its own environment,
    which is set by the gateway state — a different module, different state, not
    covered by any of the assertions above. Ship the signer without that env var and
    the result is not "unsigned commands are applied" (the verifier fails closed) but
    a total command outage in which every genuine human approval quarantines as
    `no_verification_key`. Fail-closed is correct and still an outage, so the wiring
    is pinned here, next to the grants it depends on.

    Text assertions against the Terraform source, matching the approach the rest of
    this file already takes for policy shape: these files are not `terraform plan`-ed
    in CI, so source inspection is what catches the regression that lands in a diff.
    """

    GATEWAY_INFRA = Path(__file__).resolve().parents[3] / "gateway" / "infra"
    TICK_MODULE = GATEWAY_INFRA / "modules" / "orchestration-tick"
    ENV_VAR = "ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN"

    def _read(self, path: Path) -> str:
        assert path.is_file(), f"missing {path} — this test's path arithmetic is stale, fix it rather than skipping"
        return path.read_text(encoding="utf-8")

    def test_the_tick_lambda_sets_the_env_var_the_verifier_reads(self):
        """The env var name must be the one `command_attribution.py` looks up."""
        main = self._read(self.TICK_MODULE / "main.tf")

        assert self.ENV_VAR in main, f"the tick's Lambda environment does not set {self.ENV_VAR}; the verifier would find no key and quarantine every command"
        assert f"{self.ENV_VAR} = var.engine_command_signing_key_secret_arn" in main

    def test_the_env_var_name_matches_both_implementations(self):
        """Signer and verifier deliberately read the SAME name; Terraform must use it too.

        Three independent spellings of one variable (signer, verifier, Terraform) is how
        a rename lands in two places and breaks the third.
        """
        verifier = self._read(Path(__file__).resolve().parents[3] / "gateway" / "src" / "orchestration" / "command_attribution.py")
        signer = self._read(Path(__file__).resolve().parents[1] / "lambda" / "common" / "command_signing.py")

        expected = f'SIGNING_KEY_SECRET_ARN_ENV = "{self.ENV_VAR}"'
        assert expected in verifier
        assert expected in signer

    def test_the_gateway_root_threads_the_arn_to_the_tick_module(self):
        """A module variable nothing passes is a variable that is always empty."""
        root = self._read(self.GATEWAY_INFRA / "main.tf")

        assert "engine_command_signing_key_secret_arn = var.orchestration_engine_command_signing_key_secret_arn" in root

    def test_the_gateway_root_declares_the_variable(self):
        variables = self._read(self.GATEWAY_INFRA / "variables.tf")

        assert 'variable "orchestration_engine_command_signing_key_secret_arn"' in variables

    def test_it_defaults_to_empty_so_an_unwired_environment_fails_closed(self):
        """Empty must remain the default on both the root and the module.

        A default pointing at some environment's real ARN would make a fresh state
        silently reference another environment's key.
        """
        module_vars = self._read(self.TICK_MODULE / "variables.tf")

        assert 'variable "engine_command_signing_key_secret_arn"' in module_vars
        block = module_vars.split('variable "engine_command_signing_key_secret_arn"', 1)[1]
        assert 'default     = ""' in block.split("\nvariable ", 1)[0]

    def test_the_gateway_publishes_the_tick_role_arn_for_this_state_to_grant(self):
        """The reverse direction of the same wiring.

        `engine_command_verifier_role_arn` here names the tick's role in the key policy
        as one of exactly two decrypt principals. If the gateway state does not export
        that ARN, an operator supplies it by hand — and a wrong value is not a no-op,
        it is a real decrypt grant to whatever role was named instead.
        """
        outputs = self._read(self.GATEWAY_INFRA / "outputs.tf")

        assert 'output "orchestration_tick_role_arn"' in outputs
        assert "tick_role_arn" in outputs

    def test_this_state_still_defaults_the_verifier_grant_to_unwired(self):
        """Wiring the gateway side must not have changed the fail-closed default here."""
        variables = self._read(INFRA / "variables.tf")

        block = variables.split('variable "engine_command_verifier_role_arn"', 1)[1]
        assert 'default     = ""' in block.split("\nvariable ", 1)[0]
