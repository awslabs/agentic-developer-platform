"""A retryable worker exit must NOT ack the SQS message (issue #4369).

When a long run's GitHub installation token expires, the agent enters an
unrecoverable 401 loop: no commits, no PR, no useful failure comment — it just
burns turns until it hits the cap. The worker's auth watchdog now detects that and
exits early so the task can be retried in a pod with a fresh token.

The subtlety this file pins down: Step 13 of ``main()`` deletes the SQS message on
*any* terminal exit, success or failure (issue #2117 — deliberate, to avoid
head-of-line blocking on the FIFO group and duplicate failure comments). So a plain
non-zero exit would have *destroyed* the task rather than retried it — the exact
"ack-and-die" outcome the fix must avoid, and worse than the bug itself. The
retryable exit code is the opt-out, and it only works if the ack is skipped.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import entrypoint


class TestShouldAckMessage:
    """_should_ack_message() decides whether the task is preserved for retry."""

    def test_retryable_exit_is_not_acked(self):
        """The whole point: the message survives so SQS can redeliver it."""
        assert entrypoint._should_ack_message(entrypoint.AGENT_EXIT_RETRYABLE) is False

    def test_success_is_acked(self):
        assert entrypoint._should_ack_message(0) is True

    def test_ordinary_failure_is_still_acked(self):
        """A code bug or bad prompt retries identically, so acking stays correct.

        Guards the #2117 behaviour against being widened by this change: if every
        failure stopped being acked, one broken issue would spam its failure
        comment maxReceiveCount times and block its FIFO group throughout.
        """
        assert entrypoint._should_ack_message(1) is True

    def test_other_nonzero_codes_are_acked(self):
        for code in (2, 74, 76, 127, 137, 255):
            assert entrypoint._should_ack_message(code) is True, code

    def test_retryable_code_is_outside_the_common_failure_range(self):
        """Must not collide with codes the runtime itself produces.

        1 is a generic Node throw, 130/137/143 are signal deaths (SIGINT/SIGKILL/
        SIGTERM — e.g. an OOM kill or the pod's activeDeadlineSeconds). If the
        retryable code overlapped any of those, an unrelated crash would silently
        gain retry semantics.
        """
        assert entrypoint.AGENT_EXIT_RETRYABLE not in (0, 1, 2, 130, 137, 143)
        assert 0 < entrypoint.AGENT_EXIT_RETRYABLE < 256


class TestRetryableExitContract:
    """The exit code is a cross-language constant; both halves must agree."""

    def test_matches_the_node_workers_constant(self):
        """entrypoint.py and agent-worker.ts hardcode this number independently.

        A silent drift breaks the feature in the worst way: the worker aborts for
        retry, entrypoint.py does not recognise the code, and the message is acked
        — losing the task exactly as before the fix, with nothing in the logs
        pointing at the mismatch.
        """
        worker_src = (
            Path(__file__).resolve().parents[2] / "agent" / "src" / "agent-worker.ts"
        ).read_text(encoding="utf-8")

        match = re.search(r"const EXIT_RETRYABLE\s*=\s*(\d+)", worker_src)
        assert match, "EXIT_RETRYABLE not found in agent-worker.ts"
        assert int(match.group(1)) == entrypoint.AGENT_EXIT_RETRYABLE


class TestAckSiteUsesThePredicate:
    """The ack in main() must actually consult the predicate.

    _should_ack_message() being correct is worthless if Step 13 calls
    _delete_message() unconditionally anyway — which is precisely the pre-fix
    behaviour, so a regression here reads as "tests pass, task still lost".
    """

    def test_delete_is_guarded(self):
        src = (Path(__file__).resolve().parent.parent / "entrypoint.py").read_text(
            encoding="utf-8"
        )
        # The terminal ack in main() is the _delete_message call preceded by the
        # guard; assert the guard appears before it.
        guard_pos = src.index("_should_ack_message(exit_code)")
        ack_pos = src.index("SQS message acked and deleted")
        assert guard_pos < ack_pos
