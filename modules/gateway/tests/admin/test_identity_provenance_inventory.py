"""Offline inventory behavior, lifecycle mutation and protected-output tests."""

import copy
import importlib.util
import json
import socket
import sqlite3
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/identity_provenance_inventory.py"
TENANT = "org-private-sentinel"


@pytest.fixture
def inv(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "socket", no_network)
    spec = importlib.util.spec_from_file_location("inventory_review", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def snapshot():
    return {
        "user_identities": [
            {
                "id": "identity-1",
                "org_id": TENANT,
                "user_id": "user-1",
                "provider": "github",
                "provider_user_id": "123456789-private",
                "verification_method": "admin_manual",
                "created_at": "2026-05-01T00:00:00Z",
                "updated_at": "2026-05-01T00:00:00Z",
                "verified_at": None,
                "team_id": None,
                "is_primary": False,
            }
        ],
        "users": [{"id": "user-1", "org_id": TENANT, "is_shadow": False, "user_kind": "human"}],
        "security_audit_logs": [],
    }


def event(kind="shadow_user_created", **overrides):
    value = {
        "id": "event-1",
        "org_id": TENANT,
        "actor_id": "user-1",
        "event_type": kind,
        "created_at": "2026-05-01T00:00:01Z",
        "details": {"provider": "github", "provider_user_id": "123456789-private", "shadow_user_id": "user-1"},
    }
    value.update(overrides)
    return value


def entry(inv, snapshot):
    return inv.build_manifest(snapshot, TENANT)["entries"][0]


@pytest.mark.parametrize(
    "method", ["admin_manual", "oauth", "org_placement", "magic_link_confirmed", "self_asserted", "magic_link", "channel_placement", "", "unknown"]
)
@pytest.mark.parametrize("kind", ["human", "bot"])
def test_recorded_methods_and_user_kind_never_create_historical_proof(inv, snapshot, method, kind):
    snapshot["user_identities"][0]["verification_method"] = method
    snapshot["users"][0]["user_kind"] = kind
    before = copy.deepcopy(snapshot)
    result = inv.build_manifest(snapshot, TENANT)
    assert result["has_unresolved"]
    assert result["entries"][0]["classification"] in {"review_required", "insufficient_evidence"}
    assert result["entries"][0]["authority_action"] == "none"
    assert snapshot == before


def test_manual_on_still_shadow_without_audit_is_not_proven_or_auto_repaired(inv, snapshot):
    snapshot["users"][0]["is_shadow"] = True
    result = entry(inv, snapshot)
    assert result["classification"] == "review_required"
    assert "manual_on_still_shadow_requires_review" in result["flags"]
    assert result["row_state"]["verification_method"] == "admin_manual"


def test_full_tenant_user_provider_account_join(inv, snapshot):
    identity = snapshot["user_identities"][0]
    exact = event()
    assert inv._find_matching_events(identity, [exact]) == [exact]
    # Detect mutation deleting each required tuple component in the matcher.
    for key in ("org_id", "provider", "provider_user_id", "shadow_user_id"):
        wrong = copy.deepcopy(exact)
        if key == "org_id":
            wrong[key] = "different"
        else:
            wrong["details"][key] = "different"
        assert inv._find_matching_events(identity, [wrong]) == []
    snapshot["security_audit_logs"] = [event(details={"provider": "github", "provider_user_id": "old-different-account", "shadow_user_id": "user-1"})]
    assert entry(inv, snapshot)["evidence_ids"] == []


def test_actor_is_actual_consumer_not_invented_details_user_id(inv, snapshot):
    proof = event(
        "identity_linked",
        actor_id="other-user",
        details={"provider": "github", "provider_user_id": "123456789-private", "user_id": "user-1", "ownership_proven": True},
    )
    snapshot["security_audit_logs"] = [proof]
    assert entry(inv, snapshot)["evidence_ids"] == []
    proof["actor_id"] = "user-1"
    assert entry(inv, snapshot)["evidence_ids"] == ["event-1"]
    assert entry(inv, snapshot)["classification"] == "review_required"


@pytest.mark.parametrize("delivery", [None, "provider_dm", "provider_asserted"])
def test_delivery_labels_and_forged_claims_never_prove_or_promote(inv, snapshot, delivery):
    snapshot["security_audit_logs"] = [
        event(
            "magic_link_consumed",
            details={
                "provider": "github",
                "provider_user_id": "123456789-private",
                "user_id": "user-1",
                "delivery_method": delivery,
                "ownership_proven": True,
                "identity_id": "identity-1",
                "trusted": True,
            },
        )
    ]
    result = entry(inv, snapshot)
    assert result["classification"] == "insufficient_evidence"
    assert "consumption_event_does_not_supply_delivery_or_row_proof" in result["reasons"]
    assert result["row_state"]["verification_method"] == "admin_manual"


def test_delete_recreate_and_same_method_confirmation_stay_review_required(inv, snapshot):
    snapshot["user_identities"][0]["created_at"] = "2026-06-01T00:00:00Z"
    snapshot["security_audit_logs"] = [event()]
    result = entry(inv, snapshot)
    assert "event_before_current_row_possible_recreation" in result["flags"]
    original_verified = "2026-04-01T00:00:00Z"
    snapshot["user_identities"][0]["verified_at"] = original_verified
    snapshot["security_audit_logs"] = [event("identity_linked", id=f"link-{i}", created_at="2026-07-01T00:00:00Z") for i in range(2)]
    result = entry(inv, snapshot)
    assert "repeated_link_or_same_method_confirmation" in result["flags"]
    assert result["row_state"]["verified_at"] == original_verified


def test_timezone_lifecycle_comparison_is_chronological(inv, snapshot):
    snapshot["user_identities"][0]["created_at"] = "2026-05-01T01:00:00+02:00"
    snapshot["security_audit_logs"] = [event(created_at="2026-05-01T00:00:00Z")]
    assert "event_before_current_row_possible_recreation" not in entry(inv, snapshot)["flags"]


def test_missing_user_and_same_account_in_separate_tenants(inv, snapshot):
    snapshot["users"] = []
    assert "missing_user" in entry(inv, snapshot)["flags"]
    other = copy.deepcopy(snapshot)
    other["user_identities"][0]["org_id"] = "other-tenant"
    assert inv.build_manifest(other, "other-tenant")["entries"][0]["row_state"]["provider_user_id"] == "123456789-private"


def test_conflicting_events_removed_from_evidence_but_force_review(inv, snapshot):
    first, changed = event(), event(actor_id="changed")
    snapshot["security_audit_logs"] = [first, first, changed, first]
    result = inv.build_manifest(snapshot, TENANT)
    assert result["conflicting_event_ids"] == ["event-1"]
    assert "conflicting_audit_event" in result["entries"][0]["flags"]
    assert result["entries"][0]["evidence_ids"] == []
    assert result["has_unresolved"]


def test_permutations_identical_duplicates_and_partial_inventory_are_deterministic(inv, snapshot):
    snapshot["security_audit_logs"] = [event(), event("identity_linked", id="event-2")]
    first = inv.build_manifest(snapshot, TENANT)
    snapshot["security_audit_logs"].reverse()
    snapshot["security_audit_logs"].append(copy.deepcopy(snapshot["security_audit_logs"][0]))
    assert inv.build_manifest(snapshot, TENANT) == first
    snapshot["security_audit_logs"].pop(1)
    assert inv.build_manifest(snapshot, TENANT)["manifest_hash"] != first["manifest_hash"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "recreated"),
        ("org_id", "other"),
        ("user_id", "user-2"),
        ("provider", "slack"),
        ("provider_user_id", "other-account"),
        ("created_at", "2026-06-01T00:00:00Z"),
        ("verification_method", "oauth"),
        ("verified_at", "2026-06-01T00:00:00Z"),
        ("updated_at", "2026-06-01T00:00:00Z"),
        ("team_id", "team-2"),
        ("is_primary", True),
    ],
)
def test_exact_row_predicate_binds_all_state(inv, snapshot, field, value):
    original = snapshot["user_identities"][0]
    changed = {**original, field: value}
    assert inv.compute_row_fingerprint(original) != inv.compute_row_fingerprint(changed)


def test_null_clearing_same_method_relink_and_new_evidence_invalidate_review(inv, snapshot):
    snapshot["user_identities"][0]["verified_at"] = "2026-05-01T00:00:00Z"
    expected = inv.build_manifest(snapshot, TENANT)
    assert inv.snapshot_still_matches(expected, snapshot, TENANT)
    for mutate in (
        lambda s: s["user_identities"][0].update(verified_at=None),
        lambda s: s["user_identities"][0].update(updated_at="2026-06-01T00:00:00Z"),
        lambda s: s["users"][0].update(is_shadow=True),
        lambda s: s["security_audit_logs"].append(event()),
    ):
        changed = copy.deepcopy(snapshot)
        mutate(changed)
        assert not inv.snapshot_still_matches(expected, changed, TENANT)
        assert inv.build_manifest(changed, TENANT)["manifest_hash"] != expected["manifest_hash"]


def test_canonical_fingerprint_has_no_delimiter_or_null_string_collision(inv, snapshot):
    row = snapshot["user_identities"][0]
    left, right = {**row, "id": "a|b", "user_id": "c"}, {**row, "id": "a", "user_id": "b|c"}
    assert inv.compute_row_fingerprint(left) != inv.compute_row_fingerprint(right)
    assert inv.compute_row_fingerprint(row) != inv.compute_row_fingerprint({**row, "verified_at": "NULL"})


@pytest.mark.parametrize(
    "table,field,value",
    [
        ("user_identities", "id", []),
        ("user_identities", "provider_user_id", 123),
        ("user_identities", "verification_method", {}),
        ("users", "is_shadow", "false"),
        ("users", "org_id", "other"),
        ("user_identities", "created_at", "bad"),
        ("user_identities", "updated_at", "2026-01-01T00:00:00"),
        ("security_audit_logs", "details", "token ghp_private"),
        ("security_audit_logs", "actor_id", {}),
    ],
)
def test_malformed_types_and_cross_tenant_data_rejected(inv, snapshot, table, field, value):
    snapshot["security_audit_logs"] = [event()]
    snapshot[table][0][field] = value
    with pytest.raises(inv.SnapshotError):
        inv.build_manifest(snapshot, TENANT)


def test_missing_version_fields_and_duplicate_rows_rejected(inv, snapshot):
    del snapshot["user_identities"][0]["updated_at"]
    with pytest.raises(inv.SnapshotError):
        inv.build_manifest(snapshot, TENANT)
    snapshot["user_identities"][0]["updated_at"] = None
    snapshot["users"].append(copy.deepcopy(snapshot["users"][0]))
    with pytest.raises(inv.SnapshotError):
        inv.build_manifest(snapshot, TENANT)


def test_cli_private_output_redaction_and_no_input_mutation(inv, snapshot, tmp_path, capsys):
    snapshot["users"][0]["email"] = "pii-sentinel@example.invalid"
    snapshot["security_audit_logs"] = [
        event(
            details={
                "provider": "github",
                "provider_user_id": "123456789-private",
                "shadow_user_id": "user-1",
                "jti": "nonce-private",
                "token": "Bearer private",
                "channel_context": "channel-private",
            }
        )
    ]
    source, output = tmp_path / "private-input.json", tmp_path / "pii-path@example.invalid.json"
    source.write_text(json.dumps(snapshot))
    before = source.read_bytes()
    assert inv.main(["--snapshot", str(source), "--tenant", TENANT, "--output", str(output)]) == 1
    console = capsys.readouterr()
    text = console.out + console.err
    for sentinel in (TENANT, "123456789-private", "@example.invalid", "nonce-private", "Bearer private", "channel-private"):
        assert sentinel not in text
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    full = output.read_text()
    assert "123456789-private" in full  # explicit private manifest needs exact account
    for sentinel in ("pii-sentinel", "nonce-private", "Bearer private", "channel-private"):
        assert sentinel not in full
    assert source.read_bytes() == before


@pytest.mark.parametrize("existing", ["public-file", "symlink", "input-file"])
def test_output_never_overwrites_existing_artifact(inv, snapshot, tmp_path, existing):
    manifest = inv.build_manifest(snapshot, TENANT)
    victim, output = tmp_path / "victim", tmp_path / "output"
    victim.write_text("victim")
    if existing == "symlink":
        output.symlink_to(victim)
    elif existing == "public-file":
        output.write_text("existing")
        output.chmod(0o644)
    else:
        output = victim
    with pytest.raises(OSError):
        inv.write_manifest(manifest, str(output))
    assert victim.read_text() == "victim"
    if existing == "public-file":
        assert output.read_text() == "existing"
        assert stat.S_IMODE(output.stat().st_mode) == 0o644


def test_cli_errors_redact_values_paths_and_output_failures(inv, snapshot, tmp_path, capsys):
    source = tmp_path / "private@example.invalid"
    snapshot["users"][0]["org_id"] = "Bearer private"
    source.write_text(json.dumps(snapshot))
    assert inv.main(["--snapshot", str(source), "--tenant", TENANT]) == 2
    assert "private" not in capsys.readouterr().err
    source.write_text('{"user_identities": [], "users": [], "security_audit_logs": [], "users": []}')
    assert inv.main(["--snapshot", str(source), "--tenant", TENANT]) == 2


def test_size_limits_and_empty_inventory_status(inv, tmp_path, monkeypatch, capsys):
    source = tmp_path / "input"
    source.write_text(json.dumps({name: [] for name in inv.TABLES}))
    assert inv.main(["--snapshot", str(source), "--tenant", TENANT]) == 0
    assert "EMPTY" in capsys.readouterr().out
    monkeypatch.setattr(inv, "MAX_SNAPSHOT_BYTES", 5)
    assert inv.main(["--snapshot", str(source), "--tenant", TENANT]) == 2
    manifest = inv.build_manifest({name: [] for name in inv.TABLES}, TENANT)
    monkeypatch.setattr(inv, "MAX_OUTPUT_BYTES", 5)
    with pytest.raises(inv.SnapshotError):
        inv.write_manifest(manifest, str(tmp_path / "out"))
    assert not (tmp_path / "out").exists()


def test_documented_export_preserves_mismatched_and_missing_user_evidence():
    # Run the actual portable SELECT blocks against synthetic schema. PostgreSQL
    # accepts the same $1 bind syntax; no DB/provider connection is opened.
    doc = SCRIPT.parents[3] / "docs/security/S11-identity-provenance-inventory.md"
    queries = doc.read_text().split("```sql\n", 1)[1].split("```", 1)[0]
    db = sqlite3.connect(":memory:")
    db.create_function("jsonb_build_object", -1, lambda *args: json.dumps(dict(zip(args[::2], args[1::2]))))
    db.execute(
        "CREATE TABLE user_identities(id,org_id,user_id,provider,provider_user_id,"
        "verification_method,created_at,verified_at,updated_at,team_id,is_primary)"
    )
    db.execute("CREATE TABLE users(id,org_id,is_shadow,user_kind,bot_kind,created_at,updated_at)")
    db.execute("CREATE TABLE security_audit_logs(id,org_id,event_type,actor_id,details,created_at)")
    db.executemany(
        "INSERT INTO user_identities VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("a", TENANT, "wrong-user", "github", "account", "admin_manual", "date", None, None, None, 0),
            ("b", TENANT, "missing", "github", "other-account", "admin_manual", "date", None, None, None, 0),
            ("c", "other", "unrelated", "github", "account", "oauth", "date", None, None, None, 0),
        ],
    )
    db.executemany(
        "INSERT INTO users VALUES(?,?,?,?,?,?,?)",
        [("wrong-user", "other", 0, "human", None, None, None), ("unrelated", "other", 0, "human", None, None, None)],
    )
    statements = [s.strip() for s in queries.split(";") if s.strip()]
    identities = db.execute(statements[0], {"1": TENANT}).fetchall()
    users = db.execute(statements[1], {"1": TENANT}).fetchall()
    assert [r[0] for r in identities] == ["a", "b"]
    assert [r[0] for r in users] == ["wrong-user"]
    assert db.execute(statements[2], {"1": TENANT}).fetchall() == []
    db.execute(
        "INSERT INTO security_audit_logs VALUES(?,?,?,?,?,?)",
        (
            "audit",
            TENANT,
            "shadow_user_created",
            None,
            json.dumps(
                {
                    "provider": "github",
                    "provider_user_id": "account",
                    "shadow_user_id": "wrong-user",
                    "jti": "nonce-private",
                    "token": "Bearer private",
                    "channel_context": "private-channel",
                }
            ),
            "2026-05-01T00:00:00Z",
        ),
    )
    exported = db.execute(statements[2], {"1": TENANT}).fetchall()
    detail = json.loads(exported[0][4])
    assert detail["provider_user_id"] == "account"
    assert detail["ownership_proven"] is None
    assert not {"jti", "token", "channel_context"}.intersection(detail)
    db.close()


def test_exported_absent_ownership_claim_is_null_and_not_proof(inv, snapshot):
    snapshot["security_audit_logs"] = [
        event(
            details={
                "provider": "github",
                "provider_user_id": "123456789-private",
                "shadow_user_id": "user-1",
                "ownership_proven": None,
            }
        )
    ]
    assert entry(inv, snapshot)["classification"] == "review_required"


def test_inventory_trust_vocabulary_matches_runtime(inv):
    from src.shared.identity.verification import PROVEN_METHODS

    assert inv.PROVEN_LABELS == PROVEN_METHODS
    assert "admin_manual" in inv.UNPROVEN_LABELS
    assert "admin_attested" in inv.PROVEN_LABELS


def test_empty_optional_team_preserves_state_without_granting_proof(inv, snapshot):
    absent = inv.compute_row_fingerprint(snapshot["user_identities"][0])
    snapshot["user_identities"][0]["team_id"] = ""
    manifest = inv.build_manifest(snapshot, TENANT)
    assert manifest["has_unresolved"] is True
    assert manifest["entries"][0]["authority_action"] == "none"
    assert inv.compute_row_fingerprint(snapshot["user_identities"][0]) != absent
    snapshot["user_identities"][0]["provider_user_id"] = ""
    with pytest.raises(inv.SnapshotError, match="invalid_string_field"):
        inv.build_manifest(snapshot, TENANT)
