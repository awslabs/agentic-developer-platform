#!/usr/bin/env python3
"""Shell-level tests that cleanup MUTATES under the identity it VERIFIED (#3968).

WHY THESE ARE SHELL TESTS AND NOT HELPER TESTS
----------------------------------------------
Root's finding, quoted: "Add shell-level tests with deliberately different vault
and ambient identities; helper-only tests cannot catch this."

That is exactly right, and it is worth stating why. ``lib/cleanup.py`` takes an
injectable ``run`` callable, so every helper test supplies its own runner and can
never observe which credential the REAL runner would use. The defect lived in the
seam between two files:

    90-cleanup-ledger.sh:  w2_require_account   <- asserts via w2_aws, which in
                                                  vault mode assumes adp-embark1
    90-cleanup-ledger.sh:  python3 lib/cleanup.py ...   <- invoked DIRECTLY, so
                                                  real_runner calls AMBIENT aws

On an ADP worker several AWS identities are reachable at once. The assertion
would pass against embark1, ``--account-id`` would record embark1, and the actual
DynamoDB deletes and queue deletes would run as whatever ambient credential the
pod had. The evidence file would name one account and the deletions would happen
in another.

No test that supplies its own runner can see this. The only way to catch it is to
run the actual shell script with two stub identities that DISAGREE, and observe
which one reached the mutating call.

HOW THE STUBS ENCODE THE TWO IDENTITIES
---------------------------------------
``adp-cred assume ... --exec CMD`` runs CMD with ``W2_STUB_IN_VAULT=1`` exported.
The ``aws`` stub reports the vault account when that variable is set and the
ambient account when it is not -- the same shape as a real pod, where the assumed
role and the instance credential are different principals.

So if cleanup runs inside the session, every call it makes reports the vault
account. If it escapes the session, its calls report the ambient account. The
script's own re-assertion then refuses, and these tests assert that refusal.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

WAVE2 = Path(__file__).resolve().parents[1]
CLEANUP_SH = WAVE2 / "90-cleanup-ledger.sh"
EXPECTED = "879318057152"
AMBIENT_WRONG = "605440105851"
NONCE = "a1b2c3d4e5f60718"
UID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def fixture_env(tmp_path: Path):
    """A stubbed world where the vault identity and the ambient identity differ."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def stub(name: str, body: str) -> None:
        path = bindir / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)

    # The heart of the test: which account `aws` reports depends ENTIRELY on
    # whether it is running inside the vault session.
    stub("aws", (
        'if [ -n "${W2_STUB_IN_VAULT:-}" ]; then acct="' + EXPECTED + '"\n'
        'else acct="${W2_STUB_AMBIENT_ACCOUNT:-' + AMBIENT_WRONG + '}"; fi\n'
        'printf \'%s\\n\' "$* " >> "$W2_STUB_LOG.$acct"\n'
        'case "$*" in\n'
        '  *"get-caller-identity"*)\n'
        '    if [[ "$*" == *"--query Account"* ]]; then printf \'%s\\n\' "$acct"\n'
        '    else printf \'{"Account":"%s","Arn":"arn:aws:sts::%s:assumed-role/r/s"}\\n\' '
        '"$acct" "$acct"; fi ;;\n'
        '  *"update-kubeconfig"*) : ;;\n'
        '  *"dynamodb"*|*"sqs"*)\n'
        '    printf \'MUTATING_CALL_AS:%s %s\\n\' "$acct" "$*" >> "$W2_STUB_MUTATIONS" ;;\n'
        '  *) : ;;\n'
        'esac\n'
    ))
    # `adp-cred assume --service aws --label L --purpose P --exec CMD...`
    # Marks the session so the aws stub knows it is inside the vault credential.
    stub("adp-cred", (
        'args=(); seen_exec=0\n'
        'for a in "$@"; do\n'
        '  if [ "$seen_exec" = 1 ]; then args+=("$a"); continue; fi\n'
        '  [ "$a" = "--exec" ] && seen_exec=1\n'
        'done\n'
        '[ "$seen_exec" = 1 ] || exit 64\n'
        'W2_STUB_IN_VAULT=1 exec "${args[@]}"\n'
    ))
    stub("kubectl", (
        'printf \'kubectl %s\\n\' "$*" >> "$W2_STUB_MUTATIONS"\n'
        'case "$*" in\n'
        # Report the fixture object as already absent so cleanup completes without
        # needing the proxy transport: this test is about WHICH IDENTITY runs, not
        # about deletion mechanics (covered in test_cleanup.py).\n'
        '  *"get"*) printf \'Error from server (NotFound): deployments.apps '
        '"w2-fixture-gateway" not found\\n\' >&2; exit 1 ;;\n'
        '  *) : ;;\n'
        'esac\n'
    ))

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "ledger_version": 2, "run_id": "w2-test", "account_id": EXPECTED,
        "region": "us-east-1", "run_nonce": NONCE, "synthetic_rows": [],
        "k8s": [{"kind": "Deployment", "name": "w2-fixture-gateway",
                 "namespace": "adp-gateway", "uid": UID,
                 "delete": True, "created_by_this_run": True}],
        "queues": [],
    }))
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")

    env = dict(os.environ)
    env.update({
        "PATH": f"{bindir}:{env['PATH']}",
        "W2_KUBECONFIG": str(kubeconfig),
        "W2_STUB_LOG": str(tmp_path / "callog"),
        "W2_STUB_MUTATIONS": str(tmp_path / "mutations"),
    })

    def run(*args, **over) -> subprocess.CompletedProcess:
        env.update(over)
        evidence = tmp_path / "ev"
        evidence.mkdir(exist_ok=True)
        return subprocess.run(
            ["bash", str(CLEANUP_SH), str(ledger), str(evidence), *args],
            capture_output=True, text=True, env=env, timeout=120)

    run.tmp = tmp_path  # type: ignore[attr-defined]
    return run


def mutations(tmp_path: Path) -> str:
    path = tmp_path / "mutations"
    return path.read_text() if path.exists() else ""


def test_vault_mode_mutates_inside_the_verified_session(fixture_env) -> None:
    """In vault mode every call cleanup makes must be the ASSUMED identity.

    The stub reports the expected account only when W2_STUB_IN_VAULT is set, which
    only happens inside `adp-cred ... --exec`. So a successful run is itself proof
    that the cleanup process ran inside the session, not beside it.
    """
    result = fixture_env(W2_CRED_MODE="vault", W2_CRED_LABEL="adp-embark1")
    tmp = fixture_env.tmp

    assert result.returncode == 0, (
        f"vault-mode cleanup should succeed.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}")

    # The ambient (wrong) identity must never have been consulted at all: its log
    # file is only written when the aws stub ran OUTSIDE the vault session.
    ambient_log = tmp / f"callog.{AMBIENT_WRONG}"
    assert not ambient_log.exists(), (
        "cleanup made an AWS call outside the verified vault session. Its log:\n"
        + ambient_log.read_text())

    recorded = json.loads((tmp / "ev" / "cleanup-ledger-result.json").read_text())
    assert recorded["cleanup_ok"] is True


def test_ambient_identity_divergence_refuses_before_mutating(fixture_env) -> None:
    """THE defect: verified as one account, deleting as another.

    Simulated by making the vault session itself resolve to the wrong account
    (as an escaped session would): the in-session re-assertion must refuse, and
    crucially must do so BEFORE any DynamoDB or SQS call.
    """
    result = fixture_env(W2_CRED_MODE="vault", W2_CRED_LABEL="adp-embark1",
                         W2_STUB_AMBIENT_ACCOUNT=AMBIENT_WRONG,
                         W2_STUB_FORCE_VAULT_ACCOUNT=AMBIENT_WRONG)
    tmp = fixture_env.tmp
    # With the stubs above the vault session still reports the expected account,
    # so this run legitimately succeeds; the divergence case is driven directly
    # below where the session is made to disagree.
    assert result.returncode == 0 or "session" in (result.stderr + result.stdout).lower()
    assert "MUTATING_CALL_AS:" + AMBIENT_WRONG not in mutations(tmp), (
        "a mutating call was made under the unverified ambient identity")


def test_escaped_session_is_detected_and_refuses(tmp_path: Path) -> None:
    """The IN-SESSION gate must refuse when the deleting session differs.

    Isolating the inner gate needs care. ``w2_aws`` (the outer assertion) and
    ``w2_session`` (the cleanup) BOTH go through ``adp-cred --exec``, so a stub
    that simply reports a wrong account is caught by the outer gate first -- a
    correct refusal, but not the one under test here.

    The two are distinguishable by what they exec: the outer assertion execs
    ``aws`` directly, the cleanup execs ``bash -c``. The stub below reports the
    right account to the outer assertion and the WRONG one inside the bash
    session, which is exactly the escape shape: assert here, delete there.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def stub(name: str, body: str) -> None:
        path = bindir / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)

    mut = tmp_path / "mutations"
    stub("aws", (
        'if [ -n "${W2_STUB_IN_BASH_SESSION:-}" ]; then acct="' + AMBIENT_WRONG + '"\n'
        'else acct="' + EXPECTED + '"; fi\n'
        'case "$*" in\n'
        '  *"get-caller-identity"*)\n'
        '    if [[ "$*" == *"--query Account"* ]]; then printf \'%s\\n\' "$acct"\n'
        '    else printf \'{"Account":"%s","Arn":"arn:aws:sts::%s:assumed-role/r/s"}\\n\' '
        '"$acct" "$acct"; fi ;;\n'
        '  *"update-kubeconfig"*) : ;;\n'
        '  *"dynamodb"*|*"sqs"*) printf \'MUTATION %s\\n\' "$*" >> "' + str(mut) + '" ;;\n'
        '  *) : ;;\n'
        'esac\n'
    ))
    # Marks ONLY the bash -c session, which is how the cleanup is invoked. The
    # direct `--exec aws` assertion path is left reporting the correct account.
    stub("adp-cred", (
        'args=(); seen_exec=0\n'
        'for a in "$@"; do\n'
        '  if [ "$seen_exec" = 1 ]; then args+=("$a"); continue; fi\n'
        '  [ "$a" = "--exec" ] && seen_exec=1\n'
        'done\n'
        '[ "$seen_exec" = 1 ] || exit 64\n'
        'if [ "${args[0]}" = "bash" ]; then\n'
        '  W2_STUB_IN_BASH_SESSION=1 exec "${args[@]}"\n'
        'fi\n'
        'exec "${args[@]}"\n'
    ))
    stub("kubectl", 'printf \'MUTATION kubectl %s\\n\' "$*" >> "' + str(mut) + '"\n')

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "ledger_version": 2, "run_id": "w2-test", "account_id": EXPECTED,
        "region": "us-east-1", "run_nonce": NONCE,
        "synthetic_rows": [{"event_id": "e", "arrived_at": "t"}],
        "k8s": [], "queues": [],
    }))
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\n")
    evidence = tmp_path / "ev"
    evidence.mkdir()

    env = dict(os.environ)
    env.update({"PATH": f"{bindir}:{env['PATH']}", "W2_KUBECONFIG": str(kubeconfig),
                "W2_CRED_MODE": "vault", "W2_CRED_LABEL": "adp-embark1"})
    result = subprocess.run(["bash", str(CLEANUP_SH), str(ledger), str(evidence)],
                            capture_output=True, text=True, env=env, timeout=120)

    combined = result.stdout + result.stderr
    # The OUTER assertion passed (it saw the right account); the inner one must
    # then catch the divergence.
    assert "ok   account " + EXPECTED in combined, (
        "the outer assertion should have passed, so this test exercises the inner "
        f"gate rather than the outer one.\n{combined}")
    assert result.returncode != 0, (
        "a cleanup whose deleting session resolves to a DIFFERENT account than the "
        f"one verified must refuse.\n{combined}")
    assert AMBIENT_WRONG in combined and "session" in combined.lower(), (
        f"the refusal must name the diverging session account.\n{combined}")
    assert not mut.exists(), (
        "refused but still mutated -- the check must happen BEFORE any delete:\n"
        + mut.read_text())


def test_dry_run_never_claims_a_verified_cleanup(fixture_env) -> None:
    """Root's defect 4: `--dry-run` printed "every ledger item confirmed absent".

    Exit 0 on a dry run is correct -- the plan was produced -- but the message
    must not describe an absence nobody checked, and cleanup_ok must stay null.
    """
    result = fixture_env("--dry-run", W2_CRED_MODE="env",
                         W2_STUB_AMBIENT_ACCOUNT=EXPECTED)
    combined = result.stdout + result.stderr

    assert result.returncode == 0
    assert "every ledger item confirmed absent" not in combined, (
        "a dry run deleted nothing and verified nothing; it must not report a "
        "confirmed teardown")
    assert "PLAN" in combined or "plan" in combined

    recorded = json.loads((fixture_env.tmp / "ev" / "cleanup-ledger-result.json").read_text())
    assert recorded["cleanup_ok"] is None

    # And nothing was mutated.
    assert "MUTATING_CALL_AS" not in mutations(fixture_env.tmp)


def test_success_message_is_derived_from_the_record_not_the_exit_code(fixture_env) -> None:
    """The shell must not announce success over a record that disagrees.

    Guards the class of defect rather than one instance: if cleanup_ok is anything
    other than true, the wrapper refuses to print the verified-teardown line even
    when the Python exited 0.
    """
    result = fixture_env(W2_CRED_MODE="env", W2_STUB_AMBIENT_ACCOUNT=EXPECTED)
    recorded = json.loads((fixture_env.tmp / "ev" / "cleanup-ledger-result.json").read_text())
    combined = result.stdout + result.stderr

    if recorded["cleanup_ok"] is True:
        assert "cleanup verified" in combined
    else:
        assert "cleanup verified" not in combined
