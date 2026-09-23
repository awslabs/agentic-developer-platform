#!/usr/bin/env python3
"""Offline historical counterexamples for the 2026-09-17 rework audit.

Run from an ADP checkout containing the pinned Git objects. No network, AWS,
real credentials or user configuration are used. These reproduce old failures;
they are not current-product regression tests or evidence of live acceptance.

TRUST BOUNDARY
--------------
Reproducing how a fixed bug actually behaved requires running the historical
code, so this helper compiles definitions read out of Git history. Everything it
executes is constrained as follows, and each constraint is enforced rather than
assumed:

1. **Content-addressed pins, verified before compilation.** Every source is
   pinned by full 40-hex commit ID *and* by the SHA-256 of the exact file bytes
   (``PINNED_SOURCES``). The digest is checked before anything is parsed, so a
   checkout whose object store yields different bytes fails closed instead of
   silently reproducing something else. Abbreviated revisions are refused: a
   short prefix is a lookup key resolved against whatever objects happen to be
   present locally, not an identity, and a prefix in the 24-32 bit range these
   pins previously used is cheap to target deliberately (a chosen 6-hex prefix
   is seconds of single-core work). The digest, not the revision name, is what
   authorizes execution here.

2. **Definitions only.** Only module-level ``def``/``class`` nodes whose names
   were explicitly requested are kept; top-level statements in the historical
   file are discarded and never run. On its own this is a narrowing measure, not
   a boundary -- the retained functions *are* then called -- which is why the
   digest check above is what the safety argument rests on.

3. **Explicit namespace per call site.** The compiled definitions get only the
   names their caller passes in, not a copy of this module's globals. Ambient
   access to ``os``, ``subprocess``, ``sys`` and ``tempfile`` is not inherited
   by accident; a call site that needs one supplies it deliberately.

Changing a pin means recording its new content digest. ``verify_pins`` prints
the current digests for that purpose.
"""

import ast
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

# (full 40-hex commit ID, repository path) -> SHA-256 of that file's exact bytes.
# Both halves are required: the commit ID names the revision, and the digest is
# what is actually verified before any of it is compiled.
PINNED_SOURCES = {
    ("3d22b4a039f782650965714aeecc2370b6919b99",
     "modules/gateway/cli/adp_common.py"):
        "187e91c7934718faf76be432dd96eca4d2fdd79b23d6b4b7190263d051531706",
    ("18ff18db70cc53e665f5fc100de85a95ab268cd0",
     "modules/gateway/cli/adp-github-admin.py"):
        "54d374e5f0974ecc6486b940aebfb4cf274d4472c0a3cc31b4f70c78429855fc",
    ("bf45250531089233491e25c777d88408ac13ea09",
     "modules/gateway/cli/adp-github.py"):
        "39f64a2bbc78d315680fd40544c5818ffd8aa4e40d85ccdac15e62dd84203394",
    ("3c88c8a22f8f65f7ca64602a0116098ee9c2debe",
     "tests/e2e/cli_uplift/remote/personal_inference.py"):
        "6da27c153a5085133c93b7dca74dd22a0f12336aed78fd88e8105a5f4660c8a0",
}

_FULL_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")


class UntrustedHistoricalSource(Exception):
    """A pinned historical source did not match its recorded content digest."""


def pinned_source(revision, path):
    """Return the bytes of ``path`` at ``revision``, or raise.

    Refuses anything not pinned by full object ID with a recorded digest, and
    refuses content whose digest does not match. Returns bytes: decoding is the
    caller's concern, and hashing must happen on the bytes Git produced.
    """
    if not _FULL_OBJECT_ID.match(revision):
        raise UntrustedHistoricalSource(
            f"{revision!r} is not a full 40-hex object ID; an abbreviated "
            "revision resolves against whatever objects the local checkout "
            "happens to contain and is not a content identity"
        )
    expected = PINNED_SOURCES.get((revision, path))
    if expected is None:
        raise UntrustedHistoricalSource(
            f"{path} at {revision} is not in PINNED_SOURCES; add its content "
            "digest before executing it"
        )
    # Fixed argv, no shell. `git cat-file blob` is used rather than `git show`
    # so the raw object bytes are hashed without any textual conversion.
    found = subprocess.run(  # nosec B603 - fixed argv, no shell, read-only
        ["git", "cat-file", "blob", f"{revision}:{path}"],
        check=True, capture_output=True,
    ).stdout
    actual = hashlib.sha256(found).hexdigest()
    if actual != expected:
        raise UntrustedHistoricalSource(
            f"{path} at {revision} hashes to {actual}, expected {expected}; "
            "refusing to execute unverified historical content"
        )
    return found


def historical_functions(revision, path, names, **bindings):
    """Compile the named historical definitions from a verified pinned source.

    ``bindings`` is the complete namespace the definitions will see, plus each
    other. Nothing from this module's globals is inherited.
    """
    source = pinned_source(revision, path).decode("utf-8")
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, (ast.FunctionDef, ast.ClassDef))
             and node.name in names]
    assert {node.name for node in nodes} == set(names)
    namespace = dict(bindings)
    # Executing historical definitions is this helper's purpose; it cannot be
    # replaced by a safer analysis path, because the audit question is how the
    # old code behaved when run. What bounds it is pinned_source() above --
    # verified content digest, full object ID -- and the explicit namespace,
    # not a trusted-by-convention revision name.
    exec(  # nosec B102 # nosemgrep: tmp.gitlab.bandit.B102
        compile(ast.Module(body=nodes, type_ignores=[]), f"{path}@{revision}", "exec"),
        namespace,
    )
    return SimpleNamespace(**namespace)


def verify_pins():
    """Check every pin against the local object store; report digests.

    Use this after changing a pin: it prints the digest to record.
    """
    for (revision, path), expected in sorted(PINNED_SOURCES.items()):
        try:
            pinned_source(revision, path)
        except UntrustedHistoricalSource as exc:
            print(f"MISMATCH {path}@{revision[:12]}: {exc}")
        except subprocess.CalledProcessError:
            print(f"MISSING  {path}@{revision[:12]}: object not in this checkout")
        else:
            print(f"ok       {path}@{revision[:12]} sha256={expected}")


def main():
    observed = {}
    cli = "modules/gateway/cli/"
    with tempfile.TemporaryDirectory(prefix="adp-history-probe-") as temporary:
        config = Path(temporary) / "config.json"
        config.write_text(json.dumps({"identity_pool_id": "existing-pool",
                                      "gateway_url": "https://example.invalid"}))
        # save_session/write_json/private_directory build the config path, set
        # file modes and stamp token expiry, so os/stat/time/Path/json are the
        # names this set genuinely needs. Passed explicitly, not inherited.
        common = historical_functions(
            "3d22b4a039f782650965714aeecc2370b6919b99", cli + "adp_common.py",
            ["CliError", "private_directory", "write_json", "save_session"],
            config_path=lambda: config,
            Path=Path, json=json, os=os, stat=stat, tempfile=tempfile, time=time,
        )
        common.save_session(dict(client_id="fixture", user_pool_id="fixture",
                                 region="us-east-1", access_token="synthetic",
                                 id_token="synthetic", refresh_token="synthetic",
                                 expires_in=60))
        value = json.loads(config.read_text())["identity_pool_id"]
        assert value == "", "Pinned failure no longer reproduced"
        observed["existing_identity_pool_after_login"] = value

    # owner_choice warns on stderr (sys) and, on the interactive path this probe
    # does not take, would prompt via ask(). Bind ask to a refusal so a future
    # edit that reaches it fails loudly instead of blocking on stdin.
    def _no_prompt(*args, **kwargs):
        raise AssertionError("historical probe must not prompt interactively")

    admin = historical_functions(
        "18ff18db70cc53e665f5fc100de85a95ab268cd0",
        cli + "adp-github-admin.py", ["owner_choice"],
        CliError=common.CliError, ask=_no_prompt, sys=sys,
    )
    with contextlib.redirect_stderr(io.StringIO()):
        value = admin.owner_choice(SimpleNamespace(
            owner="user", github_org="fixture-org", org="fixture-tenant"), False)
    assert value == ("user", None)
    observed["contradictory_owner_and_org"] = value

    # Supply only API/state boundaries. Keep the actual connect, matching,
    # provenance, state-clearing and output code in the exercised path.
    saved = {"gateway_url": "https://example.invalid",
             "requested_repository": "fixture-org/repo", "awaiting": "approval"}
    writes = []
    boundary = SimpleNamespace(
        read_state=lambda name: saved,
        write_state=lambda name, value: writes.append(value),
        envelope=lambda status, command, detail, *rest: {"status": status},
    )
    # Sibling module-level helpers of the historical file (owner_matches,
    # save_request, approval_next_action, unavailable_envelope) are deliberately
    # left unbound: they sit off the dry-run path exercised here and were not in
    # the namespace before either, so omitting them keeps behaviour identical.
    user = historical_functions(
        "bf45250531089233491e25c777d88408ac13ea09", cli + "adp-github.py",
        ["platform_app_missing", "connect", "parse_repo", "connections",
         "grants_repo", "repositories_of", "repositories_proven", "detail_of",
         "repository_access", "pending_request", "clear_request"],
        common=boundary, CliError=common.CliError, NAME="fixture-state",
        CONNECTIONS="/fixture-connections", _REPO=re.compile(r"^([^/]+)/([^/]+)$"),
        sys=sys,
    )
    value = user.platform_app_missing(common.CliError(
        "ADP returned HTTP 503 (gateway_unavailable)."))
    assert value is True
    observed["infrastructure_503_classified_as_missing_app"] = value
    api = SimpleNamespace(base=saved["gateway_url"], request=lambda *args: {
        "connections": [{"repositories": ["fixture-org/repo"],
                         "verification": {"repositories_live": True}}]})
    user.connect(SimpleNamespace(repo="fixture-org/repo", org=None, dry_run=True), api)
    assert writes == [{}]
    observed["dry_run_reuse_state_writes"] = writes

    # One unrelated successful row is all the API returns; no Codex request
    # exists. Run the real selector and polling predicate, without waiting.
    row = {"status_code": 200, "timestamp": "2026-09-17T10:00:00Z",
           "request_id": "unrelated-claude-request", "model": "claude"}
    boundary = SimpleNamespace(
        api=lambda *args: (200, {"items": [row]}),
        load_session=lambda config: {"access_token": "synthetic"},
        wait_for=lambda predicate, **kwargs: predicate(),
    )
    inference = historical_functions(
        "3c88c8a22f8f65f7ca64602a0116098ee9c2debe",
        "tests/e2e/cli_uplift/remote/personal_inference.py",
        ["_usage_record"], common=boundary,
    )
    value = inference._usage_record(
        {"org_id": "fixture-org", "test_user_id": "fixture-user"},
        "this-run-codex", after="2026-09-17T09:59:00Z",
    )
    assert value == row
    observed["codex_marker_selects_unrelated_claude_usage"] = value["request_id"]
    print(json.dumps({"historical_counterexamples_reproduced": 5,
                      "observed": observed}, indent=2))


if __name__ == "__main__":
    main()
