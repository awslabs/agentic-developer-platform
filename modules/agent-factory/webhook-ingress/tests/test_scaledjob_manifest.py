"""Regression guards for the agent-submit KEDA ScaledJob manifest (issue #4031).

The ScaledJob is a heredoc-embedded YAML manifest in ``infra/scaledjob.tf``
(applied via kubectl local-exec — see the comment at the top of that file for
why not ``kubernetes_manifest``). There is no terraform-plan or cluster
assertion harness for it, so these tests read the ``.tf`` source directly and
assert the scaling invariants as text. That keeps them runnable in the default
unit suite: no AWS, no cluster, no terraform binary, no new dependencies.

The invariant worth guarding is NOT the value of one field — it is that
``scaleOnInFlight`` and ``scalingStrategy`` stay ABSENT. Both look like obvious
tuning wins for a queue that visibly over-scales, and both are regressions for
this consumer. Full arithmetic lives in the scaledjob.tf comment and in issue
#4031; the failure messages below restate enough to stop a re-fix at review
time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCALEDJOB_TF = (
    Path(__file__).resolve().parents[1] / "infra" / "scaledjob.tf"
)


@pytest.fixture(scope="module")
def scaledjob_tf() -> str:
    """Raw text of infra/scaledjob.tf."""
    assert SCALEDJOB_TF.is_file(), f"ScaledJob terraform not found: {SCALEDJOB_TF}"
    return SCALEDJOB_TF.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def manifest_body(scaledjob_tf: str) -> str:
    """The keda_scaledjob_yaml heredoc body, with comment lines stripped.

    Stripping ``#`` comments matters: this file documents the rejected options
    by name, so a naive substring search for ``scaleOnInFlight`` would match
    the prose warning against it rather than a live setting.
    """
    start = scaledjob_tf.index("keda_scaledjob_yaml = <<-YAML")
    end = scaledjob_tf.index("YAML", scaledjob_tf.index("\n", start))
    body = scaledjob_tf[start:end]
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )


class TestNoOpPodHistoryIsBounded:
    """Issue #4031: completed no-op pods must not clutter `kubectl get pods`."""

    def test_successful_jobs_history_limit_is_one(self, manifest_body: str):
        """Only the most recent Completed job stays visible.

        FIFO group serialization makes KEDA spawn speculative pods that receive
        nothing and exit 0. At the previous limit of 5, those clean no-ops
        accumulated in the pod list and were indistinguishable from genuinely
        crash-looping workers, which slowed diagnosis of a real failure (#4030).
        """
        match = re.search(r"^\s*successfulJobsHistoryLimit:\s*(\d+)\s*$", manifest_body, re.MULTILINE)
        assert match, "successfulJobsHistoryLimit missing from the ScaledJob manifest"
        assert match.group(1) == "1", (
            f"successfulJobsHistoryLimit is {match.group(1)}, expected 1 (issue #4031). "
            "Completed speculative no-op pods must not accumulate in "
            "`kubectl get pods -n adp-agents` — operators cannot tell them apart "
            "from crash-looping workers."
        )

    def test_failed_jobs_history_limit_retained(self, manifest_body: str):
        """Failed jobs are the diagnostic surface — do not shrink this too.

        Guards against someone symmetrically lowering both limits: failed pods
        are exactly what an operator needs to keep for post-mortem (#4030).
        """
        match = re.search(r"^\s*failedJobsHistoryLimit:\s*(\d+)\s*$", manifest_body, re.MULTILINE)
        assert match, "failedJobsHistoryLimit missing from the ScaledJob manifest"
        assert int(match.group(1)) >= 5, (
            f"failedJobsHistoryLimit is {match.group(1)}, expected >= 5. Failed agent "
            "pods must be retained for diagnosis; only SUCCESSFUL no-ops are pruned."
        )


class TestScalingSemanticsUnchanged:
    """The trigger must stay a bare queue-depth trigger (issue #4031).

    scaleOnInFlight (what the scaler COUNTS) and scalingStrategy (what the
    executor SUBTRACTS) are a matched pair. The default pairing is the only
    internally consistent one for our delete-at-end consumer.
    """

    def test_no_scale_on_in_flight(self, manifest_body: str):
        """scaleOnInFlight: "false" would starve cross-group dispatches for ~6h.

        Default strategy computes ``eff = (visible + notVisible) - running``.
        Each running worker holds exactly one message in flight, so
        ``notVisible ~= running`` and the terms cancel: ``eff ~= visible``.
        Dropping in-flight from the metric double-subtracts live runs, so a
        dispatch in a different FIFO group waits until the in-flight run ends —
        up to activeDeadlineSeconds (6h) — and does not self-heal.
        """
        assert "scaleOnInFlight" not in manifest_body, (
            "scaleOnInFlight must NOT be set on the agent-submit trigger (issue #4031). "
            "With the default strategy it double-subtracts live runs "
            "(eff = visible - running), starving a second FIFO-group dispatch for up "
            "to 6h. Today's metric is self-cancelling and correctly calibrated."
        )

    def test_no_scaling_strategy(self, manifest_body: str):
        """accurate/eager subtract pending, never running -> ~25-30s churn loop.

        For a visible-but-undeliverable FIFO message they spawn a no-op pod that
        exits in ~20s, dropping pending back to 0 and immediately respawning —
        amplifying the exact noise #4031 set out to reduce.
        """
        assert "scalingStrategy" not in manifest_body, (
            "scalingStrategy must NOT be set on the agent-submit ScaledJob (issue "
            "#4031). accurate/eager subtract pendingJobCount, never runningJobCount, "
            "producing a ~25-30s no-op respawn loop for the duration of every long run."
        )

    def test_queue_length_is_one_message_per_pod(self, manifest_body: str):
        """One pod per queued message — jobs are parallelism/completions 1."""
        assert re.search(r'^\s*queueLength:\s*"1"\s*$', manifest_body, re.MULTILINE), (
            'queueLength must remain "1" (one pod per message; the job template is '
            "parallelism: 1 / completions: 1)."
        )

    def test_no_activation_queue_length(self, manifest_body: str):
        """Default activationQueueLength (0) is what makes scale-from-zero work.

        Setting it would silently break first-message dispatch: isActive is
        ``metric > activationQueueLength``.
        """
        assert "activationQueueLength" not in manifest_body, (
            "activationQueueLength must stay at its default of 0 (issue #4031) — "
            "a non-zero value silently breaks scale-from-zero dispatch."
        )


class TestScaleFromZeroLatencyUnchanged:
    """Regression check from #4031: dispatch latency must not regress."""

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("pollingInterval", "5"),
            ("minReplicaCount", "0"),
        ],
    )
    def test_latency_critical_fields(self, manifest_body: str, field: str, expected: str):
        """pollingInterval 5s + minReplicaCount 0 = scale-from-zero in one poll."""
        match = re.search(rf"^\s*{field}:\s*(\d+)\s*$", manifest_body, re.MULTILINE)
        assert match, f"{field} missing from the ScaledJob manifest"
        assert match.group(1) == expected, (
            f"{field} is {match.group(1)}, expected {expected}. Issue #4031 requires "
            "scale-from-zero dispatch latency to be unchanged by the history-limit fix."
        )


class TestRejectedOptionsAreDocumented:
    """The trap must stay explained in-file, not just in the issue tracker.

    The next person to look at this ScaledJob will see it over-scale and reach
    for the same two knobs. The in-file rationale is the control that stops
    that, so it is worth a test.
    """

    def test_coupling_trap_documented(self, scaledjob_tf: str):
        comments = "\n".join(
            line for line in scaledjob_tf.splitlines() if line.lstrip().startswith("#")
        )
        for token in ("scaleOnInFlight", "scalingStrategy", "4031"):
            assert token in comments, (
                f"scaledjob.tf comments must explain why {token!r} is rejected/relevant "
                "(issue #4031) — otherwise the next reader re-introduces the bug."
            )
