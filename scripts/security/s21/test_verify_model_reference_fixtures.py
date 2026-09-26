"""Fixture references require exact constructor binding and a SecretId consumer."""

import copy
import hashlib
import json

import pytest
import verify_model_reference_fixtures as verifier

REFERENCE = "arn:unit-fixture"
SOURCE = (
    "from src.shared.models.vault import UserCredential\n"
    "def test_fixture():\n"
    f"    credential = UserCredential(secret_arn={REFERENCE!r})\n"
)
RECORD = {"line": 3, "constructor_line": 3, "import_line": 1}


def test_bound_model_reference_passes():
    verifier.verify_context(SOURCE, RECORD, REFERENCE)


@pytest.mark.parametrize(
    "replacement",
    [
        ("secret_arn=", "password="),
        ("UserCredential(secret", "OtherModel(secret"),
        ("src.shared.models.vault", "unrelated.module"),
        ("test_fixture():", "test_fixture(UserCredential):"),
        ("    credential =", "    UserCredential = replacement\n    credential ="),
        (f"{REFERENCE!r})", f"{REFERENCE!r} + suffix)"),
        (f"{REFERENCE!r})", f"{REFERENCE!r}, **other)"),
    ],
)
def test_literal_shape_or_keyword_alone_cannot_classify(replacement):
    source = SOURCE.replace(*replacement)
    record = dict(RECORD)
    if "UserCredential = replacement" in source:
        record.update(line=4, constructor_line=4)
    with pytest.raises(ValueError):
        verifier.verify_context(source, record, REFERENCE)


def test_inexact_literal_and_source_lines_fail():
    for candidate, record in [
        (REFERENCE + "-changed", RECORD),
        (REFERENCE, dict(RECORD, line=2)),
        (REFERENCE, dict(RECORD, constructor_line=2)),
        (REFERENCE, dict(RECORD, import_line=2)),
    ]:
        with pytest.raises(ValueError):
            verifier.verify_context(SOURCE, record, candidate)


@pytest.fixture
def consumers():
    return {
        verifier.MODEL_FILE: "class UserCredential:\n    secret_arn: str = mapped_column(String(512))\n",
        verifier.DELIVERY_FILE: (
            "from src.shared.models.vault import UserCredential\n"
            "from src.shared.services.secrets_manager import SecretsManagerHelper\n"
            "async def authorize_delivery() -> UserCredential: pass\n"
            "async def deliver_credential(*, sm: SecretsManagerHelper):\n"
            "    credential = await authorize_delivery()\n"
            "    return await asyncio.to_thread(sm.get_secret_at_version, credential.secret_arn, version_id)\n"
        ),
        verifier.SERVICE_FILE: (
            "class SecretsManagerHelper:\n"
            "    def get_secret_at_version(self, secret_arn, version_id):\n"
            "        return self._client.get_secret_value(SecretId=secret_arn, VersionId=version_id)\n"
        ),
    }


def test_complete_frozen_consumer_chain_passes(consumers):
    verifier.verify_consumers(consumers.__getitem__)


@pytest.mark.parametrize(
    "file,before,after",
    [
        (verifier.MODEL_FILE, "secret_arn:", "password:"),
        (verifier.DELIVERY_FILE, "-> UserCredential", "-> OtherModel"),
        (verifier.DELIVERY_FILE, "sm: SecretsManagerHelper", "sm: OtherHelper"),
        (verifier.DELIVERY_FILE, "credential.secret_arn", "credential.password"),
        (
            verifier.DELIVERY_FILE,
            "await authorize_delivery()",
            "await unrelated_factory()",
        ),
        (verifier.SERVICE_FILE, "SecretId=secret_arn", "SecretString=secret_arn"),
        (verifier.SERVICE_FILE, "SecretId=secret_arn", "SecretId=other_value"),
        (
            verifier.SERVICE_FILE,
            "        return",
            "        secret_arn = other\n        return",
        ),
    ],
)
def test_reference_consumer_cannot_be_replaced_by_payload_or_other_binding(
    consumers, file, before, after
):
    consumers[file] = consumers[file].replace(before, after)
    with pytest.raises(ValueError):
        verifier.verify_consumers(consumers.__getitem__)


def test_exact_original_scan_and_full_audit_join(consumers, tmp_path, monkeypatch):
    revision = "1" * 40
    record = dict(
        RECORD,
        file="fixture.py",
        selector="detect-secrets|fixture.py|ri=0",
        detector="Secret Keyword",
        candidate_hash_prefix=hashlib.sha1(REFERENCE.encode()).hexdigest()[:16],
    )
    consumers["fixture.py"] = SOURCE

    def frozen_git(source, *args):
        assert source == tmp_path and args[0] == "show"
        commit, path = args[1].split(":", 1)
        assert commit == revision
        return consumers[path].encode()

    monkeypatch.setattr(verifier, "git", frozen_git)
    digest = hashlib.sha1(REFERENCE.encode()).hexdigest()
    scan = {
        "results": {
            "fixture.py": [
                {"line_number": 3, "type": "Secret Keyword", "hashed_secret": digest}
            ]
        }
    }
    audit = {
        "results": [{"filename": "fixture.py", "lines": [3], "secrets": REFERENCE}]
    }
    receipt = {
        "source_revision": revision,
        "verified_delta": 1,
        "verified_records": [record],
        "consumer_files": [
            verifier.MODEL_FILE,
            verifier.DELIVERY_FILE,
            verifier.SERVICE_FILE,
        ],
    }
    paths = [tmp_path / (name + ".json") for name in ("scan", "audit", "receipt")]

    def run():
        for path, data in zip(paths, (scan, audit, receipt)):
            path.write_text(json.dumps(data))
        verifier.verify(tmp_path, *paths)

    run()
    scan["results"]["fixture.py"][0]["hashed_secret"] = digest[:16] + "0" * 24
    with pytest.raises(KeyError):
        run()
    scan["results"]["fixture.py"][0]["hashed_secret"] = digest
    record["selector"] = "detect-secrets|fixture.py|ri=1"
    with pytest.raises(IndexError):
        run()
    record["selector"] = "detect-secrets|fixture.py|ri=0"
    receipt["verified_records"].append(copy.deepcopy(record))
    receipt["verified_delta"] = 2
    with pytest.raises(ValueError, match="Duplicate original selector"):
        run()


@pytest.mark.parametrize(
    "name", ["fixture-name", "adp/fixture/resource", "fixture.name+suffix"]
)
def test_secret_id_names_require_same_imported_model_and_consumer_context(name):
    source = SOURCE.replace(REFERENCE, name)
    verifier.verify_context(source, RECORD, name)
    with pytest.raises(ValueError):
        verifier.verify_context(
            source.replace("secret_arn=", "password="), RECORD, name
        )


def test_function_local_import_binds_its_own_constructor():
    source = (
        "def test_fixture():\n"
        "    from src.shared.models.vault import UserCredential\n"
        f"    credential = UserCredential(secret_arn={REFERENCE!r})\n"
    )
    verifier.verify_context(source, dict(RECORD, import_line=2), REFERENCE)


@pytest.mark.parametrize(
    "source,record",
    [
        (
            ("def unrelated():\n"
            "    from src.shared.models.vault import UserCredential\n"
            "def test_fixture():\n"
            f"    credential = UserCredential(secret_arn={REFERENCE!r})\n"),
            dict(RECORD, line=4, constructor_line=4, import_line=2),
        ),
        (
            ("def test_fixture():\n"
            "    if condition:\n"
            "        from src.shared.models.vault import UserCredential\n"
            f"    credential = UserCredential(secret_arn={REFERENCE!r})\n"),
            dict(RECORD, line=4, constructor_line=4, import_line=3),
        ),
        (
            ("def test_fixture():\n"
            f"    credential = UserCredential(secret_arn={REFERENCE!r})\n"
            "    from src.shared.models.vault import UserCredential\n"),
            dict(RECORD, line=2, constructor_line=2, import_line=3),
        ),
    ],
)
def test_local_import_must_be_in_own_scope_and_precede_call(source, record):
    with pytest.raises(ValueError):
        verifier.verify_context(source, record, REFERENCE)
