"""Keyring lockstep — signer ↔ verifier ↔ the rotation runbook (issue #4539).

The golden fixture pins the *envelope* canonicalization, and both suites already
assert against it. Nothing pinned the **keyring**: the JSON document an operator
writes into Secrets Manager, which both sides parse independently with their own
`parse_keyring`. That document is the rotation procedure's entire interface.

The failure this closes is quiet and expensive. Every intermediate keyring state in
`docs/runbooks/engine-command-signing-key-rotation.md` has to parse the same way on
both sides, because the signer and the verifier read the same secret at different
moments in the rotation. If one side accepted a state the other refused, the
symptom would be: rotation appears to complete, the signer signs happily, and every
resulting command quarantines on the tick — a total command outage discovered by
metric spike rather than by a test.

So these tests drive the runbook's documented states through both real parsers and
assert they agree. Same cross-module import as
`test_provenance_policy_lockstep.py` (#4029) and `test_marker_lockstep.py` (#1696):
the Lambda is packaged separately and cannot be imported normally, so `lambda/` goes
on the path for the duration.

**No real key material appears here.** The fixture values are obvious literals.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from moto import mock_aws

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LAMBDA_ROOT = _REPO_ROOT / "modules" / "agent-factory" / "webhook-ingress" / "lambda"
_RUNBOOK = _REPO_ROOT / "docs" / "runbooks" / "engine-command-signing-key-rotation.md"
_SIGNING_TF = _REPO_ROOT / "modules" / "agent-factory" / "webhook-ingress" / "infra" / "engine-command-signing.tf"

# The keyring states the runbook produces, in rotation order. Deliberately written as
# the literal documents an operator's pipeline emits rather than built by a helper —
# a helper shared with the runbook would drift with it instead of pinning it.
SEEDED = {"active_key_id": "2026-09", "keys": {"2026-09": "seeded-material"}}
OVERLAP = {
    "active_key_id": "2026-06",
    "keys": {"2026-06": "outgoing-material", "2026-09": "incoming-material"},
    "previous_valid_until": "2026-09-17T00:00:00Z",
}
SWITCHED = {
    "active_key_id": "2026-09",
    "keys": {"2026-06": "outgoing-material", "2026-09": "incoming-material"},
    "previous_valid_until": "2026-09-17T00:00:00Z",
}
RETIRED = {"active_key_id": "2026-09", "keys": {"2026-09": "incoming-material"}}

ROTATION_STATES = {
    "seeded": SEEDED,
    "overlap_published": OVERLAP,
    "signer_switched": SWITCHED,
    "outgoing_retired": RETIRED,
}


@pytest.fixture(scope="module")
def signer():
    """The webhook-ingress signer module.

    boto3 is imported at module scope but no client is constructed at import time, so
    this needs no AWS credentials and no network.
    """
    if not _LAMBDA_ROOT.is_dir():
        pytest.fail(
            f"Lambda package not found at {_LAMBDA_ROOT}. This lockstep test's path "
            "arithmetic is stale — fix the path rather than skipping, or the two sides "
            "of the keyring contract stop being compared at all."
        )

    inserted = str(_LAMBDA_ROOT) not in sys.path
    if inserted:
        sys.path.insert(0, str(_LAMBDA_ROOT))
    try:
        from common import command_signing

        yield command_signing
    finally:
        if inserted:
            sys.path.remove(str(_LAMBDA_ROOT))


@pytest.fixture(scope="module")
def verifier():
    from src.orchestration import command_attribution

    return command_attribution


@pytest.fixture(scope="module")
def publisher():
    """The webhook-ingress row writer — the module that names the row attributes.

    Separate from the `signer` fixture because these are two different contracts:
    `command_signing` owns the envelope (what gets signed), `webhook_events` owns the
    row (where the signature is stored). The verifier has to agree with both.
    """
    if not _LAMBDA_ROOT.is_dir():
        pytest.fail(f"Lambda package not found at {_LAMBDA_ROOT}. This lockstep test's path arithmetic is stale — fix the path rather than skipping.")

    inserted = str(_LAMBDA_ROOT) not in sys.path
    if inserted:
        sys.path.insert(0, str(_LAMBDA_ROOT))
    try:
        from common import webhook_events

        yield webhook_events
    finally:
        if inserted:
            sys.path.remove(str(_LAMBDA_ROOT))


class TestEveryRotationStateParsesIdenticallyOnBothSides:
    """The signer and the verifier must never disagree about a keyring.

    Disagreement in one direction (signer accepts, verifier refuses) is a command
    outage; in the other (verifier accepts, signer refuses) commands stop being
    signed at all. Both are silent until someone reads a metric.
    """

    @pytest.mark.parametrize("state", sorted(ROTATION_STATES), ids=sorted(ROTATION_STATES))
    def test_the_parsed_result_is_the_same_tuple(self, signer, verifier, state):
        document = json.dumps(ROTATION_STATES[state])

        assert signer.parse_keyring(document) == verifier.parse_keyring(document)

    def test_the_active_key_is_the_one_the_runbook_says_it_is(self, signer, verifier):
        """Guards against a parser reading `active_key_id` as "first key" or "newest".

        Step 3 publishes both keys while STILL signing with the outgoing one. If either
        side inferred the active key rather than reading the field, that step would
        switch the signer early and the overlap window would protect nothing.
        """
        document = json.dumps(OVERLAP)

        assert signer.parse_keyring(document)[0] == "2026-06"
        assert verifier.parse_keyring(document)[0] == "2026-06"

    def test_a_seeded_keyring_declares_no_overlap_window(self, signer, verifier):
        """A first seed has no previous key, so a window naming none would be noise.

        `None` rather than `""`: the verifier's stale-key check branches on falsiness,
        and an empty-string window that parsed as "declared" would be treated as an
        unparseable deadline instead of an absent one.
        """
        document = json.dumps(SEEDED)

        assert signer.parse_keyring(document)[2] is None
        assert verifier.parse_keyring(document)[2] is None


class TestBothSidesRefuseTheSameBadKeyrings:
    """A state one side accepts and the other refuses is the outage described above."""

    @pytest.mark.parametrize(
        "label,document",
        [
            ("not_json", "this is not json"),
            ("json_but_not_an_object", '["2026-09"]'),
            ("no_active_key_id", '{"keys": {"2026-09": "material"}}'),
            ("blank_active_key_id", '{"active_key_id": "   ", "keys": {"2026-09": "m"}}'),
            ("no_keys_object", '{"active_key_id": "2026-09"}'),
            ("keys_is_not_an_object", '{"active_key_id": "2026-09", "keys": []}'),
            ("active_id_has_no_material", '{"active_key_id": "2026-09", "keys": {"2026-06": "m"}}'),
            ("empty_value", ""),
            ("whitespace_value", "   "),
        ],
    )
    def test_neither_side_accepts_it(self, signer, verifier, label, document):
        with pytest.raises(Exception):  # noqa: B017 — distinct exception types by design
            signer.parse_keyring(document)
        with pytest.raises(Exception):  # noqa: B017
            verifier.parse_keyring(document)

    def test_a_half_rotated_keyring_drops_the_placeholder_id_on_both_sides(self, signer, verifier):
        """An operator who seeded one id and left the other as a placeholder.

        The placeholder id must become UNKNOWN rather than usable. Dropping it is what
        makes that state refuse the affected commands instead of signing and verifying
        under a value published in this repo.
        """
        document = json.dumps(
            {
                "active_key_id": "2026-09",
                "keys": {"2026-09": "real-material", "2026-06": "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"},
                "previous_valid_until": "2026-09-17T00:00:00Z",
            }
        )

        for side in (signer, verifier):
            _, keys, _ = side.parse_keyring(document)
            assert sorted(keys) == ["2026-09"], "the placeholder id must not be usable"

    def test_the_terraform_placeholder_keyring_is_refused_by_both(self, signer, verifier):
        """The exact document `engine-command-signing.tf` seeds.

        #4128: a signature computed under a value that ships in git REPORTS SUCCESS,
        which is strictly worse than no verification. This is the assertion that an
        applied-but-unseeded environment refuses everything, and it reads the
        placeholder out of the Terraform rather than restating it — so changing the
        Terraform without changing `_PLACEHOLDER_KEYS` fails here.
        """
        assert _SIGNING_TF.is_file(), f"missing {_SIGNING_TF} — fix the path rather than skipping"
        source = _SIGNING_TF.read_text(encoding="utf-8")
        placeholder = "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND"
        assert placeholder in source, "the Terraform placeholder was renamed; update this test and both _PLACEHOLDER_KEYS"

        document = json.dumps({"active_key_id": placeholder, "keys": {placeholder: placeholder}})

        with pytest.raises(Exception):  # noqa: B017
            signer.parse_keyring(document)
        with pytest.raises(Exception):  # noqa: B017
            verifier.parse_keyring(document)


class TestTheRunbookDescribesTheseStates:
    """A runbook that drifts from the parsers is worse than no runbook.

    Light assertions on purpose — pinning prose word-for-word makes the doc unmaintainable.
    These check that the field names and the key mechanism are still the ones the code
    reads, which is what an operator copies out of it.
    """

    @pytest.fixture(scope="class")
    def runbook(self):
        assert _RUNBOOK.is_file(), f"missing {_RUNBOOK} — the rotation procedure is a #4539 deliverable"
        return _RUNBOOK.read_text(encoding="utf-8")

    @pytest.mark.parametrize("field", ["active_key_id", "keys", "previous_valid_until"])
    def test_it_documents_every_keyring_field(self, runbook, field):
        assert field in runbook

    def test_it_names_both_deploy_units(self, runbook):
        """The issue asks for an inventory of which units sign and which verify."""
        assert "command_signing.py" in runbook
        assert "command_attribution.py" in runbook

    def test_it_documents_the_fail_closed_reasons_by_their_real_names(self, runbook, verifier):
        """Reason strings are the metric dimension an operator filters on.

        A runbook naming a reason the code does not emit sends them looking for a
        CloudWatch datapoint that cannot exist.
        """
        for reason in (
            verifier.REASON_NO_KEY,
            verifier.REASON_UNKNOWN_KEY_ID,
            verifier.REASON_STALE_KEY_ID,
            # The reason a pre-signing row produces. The runbook previously called
            # this `no_signature`, which the verifier never emits — an operator
            # filtering on it would find nothing and conclude the rows had been
            # handled.
            verifier.REASON_MISSING_SIGNATURE,
        ):
            assert reason in runbook, f"runbook does not mention {reason!r}"

    def test_it_documents_how_to_count_pre_signing_rows_before_activation(self, runbook):
        """Activation step 4 has to be executable, not an instruction to "know the count".

        The first tick after activation quarantines every pre-signing pending row at
        once. Without a predicted count that spike is indistinguishable from an attack
        or a broken key, and the intuitive response to the ambiguity — roll the
        verifier back — is precisely what reopens the forgery.
        """
        assert "engine-command-index" in runbook, "the count procedure must name the sparse GSI it queries"
        assert "--select COUNT" in runbook, "the count must be read-only; a projection could print a signature or command body"
        assert "attribute_not_exists(engine_command_signature)" in runbook, "nothing distinguishes pre-signing rows from signed pending rows"

    def test_the_counted_attribute_is_the_one_the_verifier_reads(self, runbook, verifier):
        """A filter naming the wrong attribute counts zero and reads as "nothing to do"."""
        assert f"attribute_not_exists({verifier.SIGNATURE_ATTR})" in runbook

    def test_every_aws_subcommand_it_tells_an_operator_to_run_exists(self, runbook):
        """A misspelled subcommand is a runbook step that cannot be followed.

        This caught a real one: the log check said `aws logs filter-log-pattern`,
        which is not a subcommand at all — the operator would get "Invalid choice"
        at the exact moment they were trying to confirm the quarantine spike matched
        the prediction, and the plausible reading of that failure is "the check is
        broken, proceed anyway".

        Checked against botocore's own service models rather than a hand-kept
        allowlist, so the test stays honest as the runbook grows: a new command is
        validated on its merits instead of needing an entry added next to it.
        """
        import botocore.session

        session = botocore.session.get_session()
        found = set(re.findall(r"aws\s+([a-z0-9-]+)\s+([a-z0-9-]+)", runbook))
        assert found, "no `aws <service> <command>` invocations found — the regex is stale"

        bad = []
        for service, command in sorted(found):
            try:
                model = session.get_service_model(service)
            except Exception:
                bad.append(f"{service} {command} (unknown service '{service}')")
                continue
            # botocore names operations in PascalCase; the CLI spells them kebab-case.
            wanted = "".join(part.capitalize() for part in command.split("-")).lower()
            if not any(op.lower() == wanted for op in model.operation_names):
                bad.append(f"{service} {command} (no such operation on '{service}')")

        assert not bad, "runbook names AWS commands that do not exist: " + "; ".join(bad)

    def test_the_tick_log_group_is_the_one_terraform_actually_creates(self, runbook):
        """The tick is the one gateway module whose name_prefix is adp-, not bedrockgw-.

        `modules/gateway/infra/main.tf` passes `name_prefix = "adp-${var.environment}"`
        to `module "orchestration_tick"` while passing `local.name_prefix`
        (`bedrockgw-<env>`) to every other module in the same state. Writing the
        habitual prefix here yields a log group that does not exist, and
        `filter-log-events` against a missing group errors rather than returning zero
        — again at the moment the operator is deciding whether to trust the verifier.
        """
        assert "/aws/lambda/adp-${ENVIRONMENT}-orchestration-tick" in runbook
        assert "bedrockgw-${ENVIRONMENT}-orchestration-tick" not in runbook

    def test_it_rules_out_a_reconcile_script(self, runbook):
        """The one action that must not be scripted.

        A script that "reconciles" a pre-signing row can only do it by signing the row
        (minting authority for a tuple nothing verified — this issue's forgery,
        performed by us) or by marking it approved. The runbook has to say so, because
        "reconcile the pending rows" reads like a request for a tool.
        """
        assert "no reconcile script" in runbook.lower()

    def test_it_never_puts_key_material_on_a_command_line(self, runbook):
        """`--secret-string "$NEW_KEY"` would put the key in argv, `ps` and shell history.

        Every rotation step must pipe the document into `file:///dev/stdin` instead. This
        is the assertion that a future edit "simplifying" a pipeline does not
        reintroduce the leak the pipelines exist to avoid.
        """
        for line in runbook.splitlines():
            if "--secret-string" not in line:
                continue
            # Shell comments explain the rule and quote the bad form to warn against it.
            if line.lstrip().startswith("#"):
                continue
            # No `$(...)` escape hatch: command substitution expands the keyring into
            # argv just as surely as a bare variable does. `file:///dev/stdin` is the
            # only acceptable form.
            assert "file:///dev/stdin" in line, f"key material on a command line: {line.strip()}"

    def test_it_warns_that_debug_prints_the_value(self, runbook):
        """The AWS CLI logs resolved parameters, so `--debug` on a put defeats the piping.

        Verified against the real CLI: the resolved `--secret-string` appears in
        `--debug` output. An operator troubleshooting a failed rotation reaches for
        `--debug` first, which is exactly when the warning has to already be there.
        """
        assert "--debug" in runbook

    def test_it_documents_the_worker_isolation_check(self, runbook):
        """The activation order requires PROVING the worker cannot read the key.

        A policy-shape test catches a bad diff; only an assume-role read catches a
        grant that arrived from somewhere the tests do not render.
        """
        assert "assume-role" in runbook
        assert "AccessDeniedException" in runbook


class TestTheRowAttributeNamesAreTheSameOnBothSides:
    """The four attributes that carry a signature from ingress to the tick.

    The golden contract pins the *envelope* — the 16 fields, their order and their
    types — and both suites assert against it. It says nothing about the *row*: the
    DynamoDB attribute names the publisher writes the signature under and the
    verifier reads it back from. Those four strings are declared independently in
    `webhook_events.py` and in `command_attribution.py`, because the Lambda zip and
    the gateway image cannot import each other.

    Nothing pinned them, and the failure mode is a silent total outage rather than a
    test failure. Rename `engine_command_signature` on one side only and both
    modules' own suites still pass — each is internally consistent — while in
    production every genuine command quarantines as `missing_signature`, because the
    verifier is reading an attribute the publisher no longer writes. Same class of
    cross-deploy-unit drift the keyring tests above close, one seam further along.
    """

    # (publisher attribute, verifier attribute). Written as an explicit pairing
    # rather than derived from either module, so a rename has to be made HERE too —
    # which is the point: this table is the contract.
    PAIRS = (
        ("ENGINE_COMMAND_SIGNATURE_ATTR", "SIGNATURE_ATTR"),
        ("ENGINE_COMMAND_KEY_ID_ATTR", "KEY_ID_ATTR"),
        ("ENGINE_COMMAND_SIGNED_PAYLOAD_ATTR", "SIGNED_PAYLOAD_ATTR"),
        ("ENGINE_COMMAND_PROTOCOL_VERSION_ATTR", "PROTOCOL_VERSION_ATTR"),
    )

    @pytest.mark.parametrize(("published", "verified"), PAIRS)
    def test_the_two_declarations_agree(self, publisher, verifier, published, verified):
        assert getattr(publisher, published) == getattr(verifier, verified)

    def test_the_protocol_version_written_is_one_the_verifier_supports(self, publisher, verifier):
        """The publisher stamps its own version; the verifier accepts a fixed set.

        A signer shipped at version 2 against a verifier that only knows version 1
        refuses every command with `unknown_protocol_version`. That is the correct
        fail-closed behaviour, but it must be a deliberate migration rather than a
        surprise, so the versions are compared here.
        """
        assert str(publisher.ENGINE_COMMAND_PROTOCOL) in verifier.SUPPORTED_PROTOCOL_VERSIONS

    def test_the_pending_marker_value_agrees(self, publisher, verifier):
        """The verifier's quarantine write is conditional on the row still being pending.

        `engine_commands.py` re-declares this status for the same deploy-unit reason.
        If the two spellings drifted, the conditional would never match and an
        unverifiable row would be re-read forever — the "not retried forever"
        property the issue asks for, lost silently.
        """
        from src.orchestration import engine_commands

        assert publisher.ENGINE_COMMAND_STATUS_PENDING == engine_commands.ENGINE_COMMAND_STATUS_PENDING
        assert publisher.ENGINE_COMMAND_STATUS_CONSUMED == engine_commands.ENGINE_COMMAND_STATUS_CONSUMED


@mock_aws
class TestARowTheSignerWroteVerifiesOnTheTick:
    """The ingress write → tick read seam, end to end through both real modules.

    Every other test here drives ONE side against the shared fixture. That proves
    canonicalization parity but not composition: it does not establish that the row
    the publisher actually emits is a row the verifier actually accepts. The gap
    between those two statements is where the attribute names, the payload
    serialization and the numeric types all live.

    This signs with the real signer, writes with the real publisher, stores the row in
    a moto DynamoDB table, reads it back and verifies it with the real verifier. The
    only substitution is the AWS backend.
    """

    KEY_ID = "2026-09"
    KEY = "lockstep-fixture-material-not-a-real-key"

    @pytest.fixture
    def keyring_env(self, signer, verifier, monkeypatch):
        """Both sides pointed at one keyring, with no AWS call.

        The secret fetch is stubbed at the module's cache rather than by mocking
        boto3: the cache is the documented seam (`reset_key_cache`), and stubbing
        boto3 would leave this test asserting against a client shape instead of the
        keyring contract.
        """
        material = self.KEY.encode("utf-8")

        # The signer caches only the ACTIVE pair, since signing never needs a retired
        # key; the verifier caches the whole keyring, since it must still accept rows
        # signed during a rotation overlap. Two shapes, one key — which is exactly the
        # asymmetry these round-trip tests have to cross.
        monkeypatch.setattr(signer, "_active", (self.KEY_ID, material), raising=False)
        monkeypatch.setattr(signer, "_loaded", True, raising=False)
        monkeypatch.setattr(verifier, "_keyring", (self.KEY_ID, {self.KEY_ID: material}, None), raising=False)
        monkeypatch.setattr(verifier, "_keyring_loaded", True, raising=False)
        yield
        signer.reset_key_cache()
        verifier.reset_key_cache()

    TABLE = "lockstep-webhook-events"

    def _write(self, publisher, **overrides):
        """Drive the real `log_event` against a real table, and read the row back.

        A moto-backed table rather than a stub client, for the reason this test
        exists: the row has to survive an actual DynamoDB round trip. A stub that
        captured the `Item` dict would skip serialization entirely and could not show
        that the signed payload is still byte-identical after storage — which is the
        `Decimal` hazard `webhook_events.py` documents at the attribute declarations.
        """
        import boto3

        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=self.TABLE,
            KeySchema=[
                {"AttributeName": "event_id", "KeyType": "HASH"},
                {"AttributeName": "arrived_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "event_id", "AttributeType": "S"},
                {"AttributeName": "arrived_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = ddb.Table(self.TABLE)

        payload = dict(
            event_id="evt-lockstep-1",
            arrived_at="2026-09-16T00:00:00Z",
            tenant_id="tenant-lockstep",
            channel="github",
            event_type="issue_comment",
            action="created",
            installation_id="55501",
            repo="aws-e/adp",
            engine_command=True,
            comment_body="@agent-engine accept",
            sender_github_id="9001",
            issue_number=4539,
            create_only=True,
        )
        payload.update(overrides)
        publisher.WebhookEventLogger(table_name=self.TABLE).log_event(**payload)

        stored = table.get_item(Key={"event_id": payload["event_id"], "arrived_at": payload["arrived_at"]}).get("Item")
        assert stored is not None, "the publisher dropped the row; nothing to verify"
        return stored

    def _sign_and_write(self, signer, publisher, **envelope_overrides):
        fields = dict(
            delivery_id="delivery-lockstep-1",
            event_type="issue_comment",
            event_id="evt-lockstep-1",
            arrived_at="2026-09-16T00:00:00Z",
            tenant_id="tenant-lockstep",
            installation_id="55501",
            repo_id=760155,
            repo="aws-e/adp",
            issue_number=4539,
            sender_github_id="9001",
            sender_type="User",
            command_body="@agent-engine accept",
            signed_at="2026-09-16T00:00:01Z",
        )
        fields.update(envelope_overrides)
        key_id, signature, envelope = signer.sign_command(**fields)
        row = self._write(
            publisher,
            engine_command_signature=signature,
            engine_command_signing_key_id=key_id,
            engine_command_signed_payload=signer.canonical_bytes(envelope).decode("utf-8"),
            **{
                k: v
                for k, v in (
                    ("event_id", fields["event_id"]),
                    ("arrived_at", fields["arrived_at"]),
                    ("tenant_id", fields["tenant_id"]),
                    ("installation_id", fields["installation_id"]),
                    ("repo", fields["repo"]),
                    ("issue_number", fields["issue_number"]),
                    ("comment_body", fields["command_body"]),
                    ("sender_github_id", fields["sender_github_id"]),
                )
            },
        )
        return row, envelope

    def test_the_written_row_verifies_and_yields_the_signed_tuple(self, signer, publisher, verifier, keyring_env, monkeypatch):
        row, envelope = self._sign_and_write(signer, publisher)

        verified = verifier.verify_row(row)

        # Authority and routing come from the signed tuple, so these are the values
        # the tick will act on.
        assert verified.tenant_id == envelope["tenant_id"]
        assert verified.installation_id == envelope["installation_id"]
        assert verified.repo == envelope["repo"]
        assert verified.issue_number == envelope["issue_number"]
        assert verified.sender_github_id == envelope["sender_github_id"]
        assert verified.sender_type == "User"
        assert verified.command_body == "@agent-engine accept"
        assert verified.key_id == self.KEY_ID

    def test_a_unicode_command_body_survives_the_round_trip(self, signer, publisher, verifier, keyring_env, monkeypatch):
        """`ensure_ascii=False` means the canonical bytes are real UTF-8.

        A row is stored and re-read as text, so a body that is not pure ASCII is
        where a stray encode/decode would break the signature. Newline included
        because comment bodies routinely have them.
        """
        body = "@agent-engine accept — ünïcode ✅\nsecond line"
        row, _ = self._sign_and_write(signer, publisher, command_body=body)

        assert verifier.verify_row(row).command_body == body

    def test_the_bot_sender_type_reaches_the_verifier_as_signed(self, signer, publisher, verifier, keyring_env, monkeypatch):
        """Bot attribution is signed, so the human-only gate cannot be bypassed by row edit."""
        row, _ = self._sign_and_write(signer, publisher, sender_type="Bot")

        assert verifier.verify_row(row).sender_type == "Bot"

    def test_an_unsigned_row_the_publisher_wrote_is_refused(self, publisher, verifier, keyring_env, monkeypatch):
        """The signing-failure path: marker written, no signature, tick refuses.

        This is the legitimate pre-signing / no-key state, and it must produce a
        refusal rather than an accepted command.
        """
        row = self._write(publisher)

        assert row["engine_command_status"] == publisher.ENGINE_COMMAND_STATUS_PENDING
        assert verifier.SIGNATURE_ATTR not in row

        with pytest.raises(verifier.AttributionError) as exc:
            verifier.verify_row(row)
        assert exc.value.reason == verifier.REASON_MISSING_SIGNATURE

    def test_replaying_the_row_onto_another_tenant_is_refused(self, signer, publisher, verifier, keyring_env, monkeypatch):
        """The signature commits to the tenant, so a moved row does not verify."""
        row, _ = self._sign_and_write(signer, publisher)
        row["tenant_id"] = "tenant-victim"

        with pytest.raises(verifier.AttributionError) as exc:
            verifier.verify_row(row)
        assert exc.value.reason == verifier.REASON_ROW_MISMATCH

    def test_editing_the_command_body_on_the_row_is_refused(self, signer, publisher, verifier, keyring_env, monkeypatch):
        """The forgery the issue is about: escalating `status` to `accept` by row edit."""
        row, _ = self._sign_and_write(signer, publisher, command_body="@agent-engine status")
        row["engine_command_body"] = "@agent-engine accept"

        with pytest.raises(verifier.AttributionError) as exc:
            verifier.verify_row(row)
        assert exc.value.reason == verifier.REASON_ROW_MISMATCH

    def test_a_row_signed_under_a_retired_key_is_refused(self, signer, publisher, verifier, keyring_env, monkeypatch):
        """Rotation is bounded: a key with no declared overlap window is stale.

        Signed while the fixture key was active, then verified against a keyring that
        has moved on. Accepting this would make every past key valid forever.
        """
        row, _ = self._sign_and_write(signer, publisher)

        rotated = ("2026-12", {"2026-12": b"newer-material"}, None)
        monkeypatch.setattr(verifier, "_keyring", rotated, raising=False)
        monkeypatch.setattr(verifier, "_keyring_loaded", True, raising=False)

        with pytest.raises(verifier.AttributionError) as exc:
            verifier.verify_row(row)
        assert exc.value.reason == verifier.REASON_UNKNOWN_KEY_ID

    def test_the_verifier_refuses_when_no_key_is_configured(self, signer, publisher, verifier, keyring_env, monkeypatch):
        """An unwired verifier quarantines rather than accepting on trust.

        This is the state a partially-wired environment is in — the signer deployed
        and `ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN` not yet set on the tick — and it
        must fail closed.
        """
        row, _ = self._sign_and_write(signer, publisher)

        monkeypatch.setattr(verifier, "_keyring", None, raising=False)
        monkeypatch.setattr(verifier, "_keyring_loaded", True, raising=False)

        with pytest.raises(verifier.AttributionError) as exc:
            verifier.verify_row(row)
        assert exc.value.reason == verifier.REASON_NO_KEY
