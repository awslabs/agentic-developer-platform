"""adp-review — the one sanctioned way to publish a formal GitHub PR review.

Issue #5350. Every pull request the engine opens is authored by the tenant's
GitHub App, and every reviewer the engine dispatched authenticated as that SAME
App. GitHub refuses APPROVE and REQUEST_CHANGES on your own pull request:

    POST /repos/{owner}/{repo}/pulls/{n}/reviews  event=APPROVE
    -> 422 {"errors":["Review Can not approve your own pull request"]}

COMMENT is accepted, but a COMMENT review does not set ``reviewDecision``. So the
reviewer's verdict had nowhere to go, and — this is the part that made the defect
survive for the whole life of the engine — *nothing in the codebase owned review
submission*. The reviewer improvised with the ``gh`` CLI, no code saw the 422, and
the reviewer never learned it had failed. It fell back to whatever remained
available: the verdict as an ordinary issue comment, or committed as files on a
branch. Both look like progress. Neither records a verdict.

This CLI exists so that failure has exactly one owner and can never be invisible
again. It:

  * requests the distinct REVIEWER App identity, so the review is not a
    self-review and a real verdict is possible;
  * submits the real review with the real verdict;
  * on the self-review 422, logs the refusal explicitly, publishes the verdict as
    a comment anyway so the analysis is not lost, and appends a notice naming the
    pending human approval — which is what
    ``rules/personas/reviewer.md`` already required and could not enforce;
  * exits with a DISTINCT status for "verdict published but not recorded", so a
    comment-only outcome can never be read by a caller as an approval.

Exit codes:
  0  the formal review was submitted and carries a verdict
  1  the review could not be published at all
  2  usage or environment error
  3  the verdict was published as a comment, but no formal verdict was recorded
     (the self-review 422 path — a human approval is pending)
"""

__version__ = "0.1.0"
