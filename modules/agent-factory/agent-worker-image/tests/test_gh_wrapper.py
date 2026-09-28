"""Tests for the gh shim — ordinary comments must not mint dispatch authority (#5120).

The defect being closed (a recurrence of #2149 through the #2159 cross-issue
path): the shim inspected any `gh issue comment <N>` / `gh pr comment <N>` aimed
at an issue other than the run's own, grepped the body for the first
`@agent-<persona>` token, seeded a dispatch pointer for the target channel, and
rewrote the body to carry an `adp-dispatch:<persona>` marker. The webhook's
bot-sender gate accepts that marker as a deliberate dispatch, so an informational
status update or a review header that merely NAMED a persona started a real agent
run nobody asked for.

These tests assert OUTCOMES against the real script, not source text:
  * the argv the real `gh` receives is byte-for-byte what the caller passed
  * the comment body reaching GitHub is unmodified — no marker, no rewrite of
    `--body` into `--body-file`
  * neither the marker helper nor the pointer seeder is invoked for an ordinary
    comment, for ANY shape of it (cross-issue, quoted mentions, multiline,
    Unicode, multiple mentions, URL and numeric targets, repo flags)
  * the token-file load (#1469) still happens, since long runs 401 without it
  * the real gh's exit code propagates unchanged

The helpers are staged as fake recorder modules on PYTHONPATH: if the shim ever
calls them again, the recorder writes a file and the assertion fails. That proves
the path is gone rather than merely unused.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WRAPPER = HERE.parent / "gh-wrapper"

# A persona mention in ordinary prose. Historically this token alone was enough
# to make the shim mint dispatch authority.
MENTION = "@agent-operations"


class Harness:
    """A staged wrapper environment: fake real-gh + fake helper recorders."""

    def __init__(self, root: Path):
        self.root = root
        self.argv_file = root / "argv.txt"
        self.marker_calls = root / "marker_calls.txt"
        self.pointer_calls = root / "pointer_calls.txt"
        self.token_file = root / "token"

    def run(self, args, *, env=None, issue_number="5120", exit_code=0):
        environ = dict(os.environ)
        environ.pop("ADP_DISPATCH_PERSONA", None)
        environ.update(
            {
                "ADP_REAL_GH": str(self.root / "fake-gh"),
                "ADP_TOKEN_FILE": str(self.token_file),
                "ADP_FAKE_GH_EXIT": str(exit_code),
                "ADP_FAKE_GH_ARGV_FILE": str(self.argv_file),
                # Everything the retired path needed to fire.
                "ISSUE_NUMBER": issue_number,
                "TARGET_REPO": "aws-e/adp",
                "ADP_CORRELATION_ID": "corr-1",
                "ADP_ROOT_HUMAN_ID": "human-1",
                "ADP_IS_HUMAN_ROOTED": "true",
                "ADP_MESSAGE_ID": "msg-1",
                "ADP_CHAIN_DEPTH": "0",
                # Fake lib.* helpers shadow the real ones for `python3 -m lib.*`
                # and for `from lib.correlation_marker import ...`.
                "PYTHONPATH": str(self.root / "pythonpath"),
                "AGENT_WORKDIR": str(self.root / "pythonpath"),
            }
        )
        if env:
            environ.update(env)
        return subprocess.run(
            ["sh", str(WRAPPER), *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=environ,
            # Non-zero is expected: several tests assert the wrapper propagates
            # the real gh's exit status, so raising here would defeat them.
            check=False,
        )

    def recorded_argv(self) -> list[str]:
        """The argv the real gh actually received, one arg per line."""
        if not self.argv_file.exists():
            return []
        text = self.argv_file.read_text(encoding="utf-8")
        return text.split("\x00")[:-1] if text else []

    def helper_invoked(self) -> bool:
        return self.marker_calls.exists() or self.pointer_calls.exists()

    def body_delivered(self) -> str:
        """The body text the real gh received via --body or --body-file."""
        argv = self.recorded_argv()
        for i, arg in enumerate(argv):
            if arg == "--body":
                return argv[i + 1]
            if arg in ("--body-file", "-F"):
                return Path(argv[i + 1]).read_text(encoding="utf-8")
        raise AssertionError(f"no body argument in {argv!r}")


@pytest.fixture
def gh(tmp_path: Path) -> Harness:
    h = Harness(tmp_path)
    h.token_file.write_text("ghs_fresh_token_from_file")

    # Fake real-gh: records argv NUL-separated (so bodies with newlines survive)
    # and exits with a caller-chosen code.
    fake = tmp_path / "fake-gh"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "with open(os.environ['ADP_FAKE_GH_ARGV_FILE'], 'w', encoding='utf-8') as fh:\n"
        "    for a in sys.argv[1:]:\n"
        "        fh.write(a + '\\x00')\n"
        "sys.stdout.write(os.environ.get('GH_TOKEN', '') + '\\n')\n"
        "sys.exit(int(os.environ.get('ADP_FAKE_GH_EXIT', '0')))\n"
    )
    fake.chmod(0o755)

    # Fake helper package: any call records itself and fails the assertion.
    libdir = tmp_path / "pythonpath" / "lib"
    libdir.mkdir(parents=True)
    (libdir / "__init__.py").write_text("")
    (libdir / "seed_trigger_pointer.py").write_text(
        "import os, sys\n"
        "open(os.environ['ADP_FAKE_POINTER_FILE'], 'a').write(repr(sys.argv) + '\\n')\n"
    )
    (libdir / "correlation_marker.py").write_text(
        "import os\n"
        "def prepend_correlation_marker(body, *, dispatch_persona=None):\n"
        "    open(os.environ['ADP_FAKE_MARKER_FILE'], 'a').write(repr(dispatch_persona) + '\\n')\n"
        "    return '<!-- adp-correlation:corr-1 adp-dispatch:%s -->\\n' % dispatch_persona + body\n"
    )
    os.environ["ADP_FAKE_POINTER_FILE"] = str(h.pointer_calls)
    os.environ["ADP_FAKE_MARKER_FILE"] = str(h.marker_calls)
    return h


# --- Argument and body preservation -------------------------------------------


@pytest.mark.parametrize("kind", ["issue", "pr"])
def test_cross_issue_mention_body_is_passed_through_verbatim(gh: Harness, kind: str):
    """The #5120 defect: cross-issue prose naming a persona used to be rewritten.

    Own issue is 5120; the comment targets 5096 and names a persona — exactly the
    shape that previously acquired an `adp-dispatch` marker.
    """
    body = f"Status update: {MENTION} finished the migration on #5105."
    result = gh.run([kind, "comment", "5096", "--body", body])

    assert result.returncode == 0
    assert gh.recorded_argv() == [kind, "comment", "5096", "--body", body]
    assert gh.body_delivered() == body
    assert "adp-dispatch" not in gh.body_delivered()
    assert not gh.helper_invoked()


def test_body_file_input_is_not_rewritten(gh: Harness, tmp_path: Path):
    """--body-file must reach gh as the caller's own path, contents untouched."""
    body = f"Review complete. {MENTION} owns the follow-up.\n"
    path = tmp_path / "review.md"
    path.write_text(body, encoding="utf-8")

    gh.run(["pr", "comment", "5118", "--body-file", str(path)])

    assert gh.recorded_argv() == ["pr", "comment", "5118", "--body-file", str(path)]
    assert path.read_text(encoding="utf-8") == body, "caller's file was mutated"
    assert gh.body_delivered() == body
    assert not gh.helper_invoked()


def test_short_body_file_flag_is_not_rewritten(gh: Harness, tmp_path: Path):
    """-F is the short form the old shim also intercepted."""
    body = f"{MENTION} please see the notes above."
    path = tmp_path / "note.md"
    path.write_text(body, encoding="utf-8")

    gh.run(["issue", "comment", "5096", "-F", str(path)])

    assert gh.recorded_argv() == ["issue", "comment", "5096", "-F", str(path)]
    assert gh.body_delivered() == body
    assert not gh.helper_invoked()


def test_same_issue_comment_unchanged(gh: Harness):
    """Same-issue comments were already exempt; they must stay exempt."""
    body = f"Progress note mentioning {MENTION}."
    gh.run(["issue", "comment", "5120", "--body", body], issue_number="5120")

    assert gh.recorded_argv() == ["issue", "comment", "5120", "--body", body]
    assert not gh.helper_invoked()


def test_url_target_unchanged(gh: Harness):
    """A URL target (the non-numeric form) must pass through untouched."""
    url = "https://github.com/aws-e/adp/issues/5096"
    body = f"Cross-linking for {MENTION}."
    gh.run(["issue", "comment", url, "--body", body])

    assert gh.recorded_argv() == ["issue", "comment", url, "--body", body]
    assert not gh.helper_invoked()


def test_repo_flag_and_extra_flags_preserved_in_order(gh: Harness):
    """Repository flags and their positions must survive exactly."""
    body = f"Deploy owner is {MENTION}."
    args = ["issue", "comment", "5096", "--repo", "aws-e/adp", "--body", body]
    gh.run(args)

    assert gh.recorded_argv() == args
    assert not gh.helper_invoked()


@pytest.mark.parametrize(
    "label,body",
    [
        ("quoted mention", f'The rule says: "{MENTION} implements the story."'),
        ("multiline", f"Line one\n\n- owner: {MENTION}\n- state: done\n"),
        ("unicode", f"Résumé — 完了 ✅ {MENTION} — naïve façade\n"),
        ("multiple mentions", f"{MENTION} handed off to @agent-developer and @agent-reviewer."),
        ("mention mid-word-ish", f"see {MENTION}, then @agent-architect."),
        ("code fence", f"```\ngh issue comment 1 --body '{MENTION}'\n```\n"),
        ("leading mention", f"{MENTION} status: the wave is complete."),
    ],
)
def test_prose_shapes_are_never_marked(gh: Harness, label: str, body: str):
    """Every prose shape that names a persona stays prose."""
    gh.run(["issue", "comment", "5096", "--body", body])

    assert gh.body_delivered() == body, f"{label}: body was modified"
    assert "adp-dispatch" not in gh.body_delivered(), f"{label}: acquired dispatch authority"
    assert not gh.helper_invoked(), f"{label}: called a dispatch helper"


def test_body_with_no_mention_unchanged(gh: Harness):
    """Regression guard for the ordinary case."""
    body = "Plain status update with no persona named."
    gh.run(["issue", "comment", "5096", "--body", body])

    assert gh.body_delivered() == body
    assert not gh.helper_invoked()


def test_non_comment_subcommands_pass_through(gh: Harness):
    """Non-comment gh verbs were never in scope and must stay untouched."""
    args = ["pr", "create", "--title", f"fix: {MENTION} path", "--body", MENTION]
    gh.run(args)

    assert gh.recorded_argv() == args
    assert not gh.helper_invoked()


# --- Behavior the wrapper must KEEP -------------------------------------------


def test_fresh_token_is_exported_from_token_file(gh: Harness):
    """Issue #1469: without this, runs longer than the 1h token expiry 401."""
    result = gh.run(["issue", "comment", "5096", "--body", "hello"])

    assert "ghs_fresh_token_from_file" in result.stdout


def test_missing_token_file_is_not_fatal(gh: Harness):
    """A missing token file must not break gh — the env may already carry auth."""
    gh.token_file.unlink()
    result = gh.run(["issue", "comment", "5096", "--body", "hello"])

    assert result.returncode == 0
    assert gh.recorded_argv() == ["issue", "comment", "5096", "--body", "hello"]


@pytest.mark.parametrize("code", [0, 1, 2, 42])
def test_exit_code_propagates(gh: Harness, code: int):
    """The wrapper execs gh; the caller must see gh's own status."""
    result = gh.run(["issue", "comment", "5096", "--body", "x"], exit_code=code)

    assert result.returncode == code


def test_helper_failure_cannot_break_a_comment(gh: Harness, tmp_path: Path):
    """Even with the helper modules unimportable, the comment must still post.

    The old path was fail-soft by design; the new path cannot fail at all,
    because it no longer calls a helper.
    """
    broken = tmp_path / "broken"
    (broken / "lib").mkdir(parents=True)
    (broken / "lib" / "__init__.py").write_text("raise RuntimeError('boom')\n")

    result = gh.run(
        ["issue", "comment", "5096", "--body", MENTION],
        env={"PYTHONPATH": str(broken), "AGENT_WORKDIR": str(broken)},
    )

    assert result.returncode == 0
    assert gh.recorded_argv() == ["issue", "comment", "5096", "--body", MENTION]


def test_wrapper_source_has_no_dispatch_injection(gh: Harness):
    """Belt-and-braces: the retired mechanism's names must not reappear.

    The behavioral tests above are the real guarantee; this catches a
    reintroduction that a future body-shape test might not cover.
    """
    source = WRAPPER.read_text(encoding="utf-8")
    # Only the explanatory comment may mention these; no executable use.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    for retired in ("seed_trigger_pointer", "prepend_correlation_marker", "adp-dispatch"):
        assert retired not in code, f"{retired} reintroduced into gh-wrapper"
