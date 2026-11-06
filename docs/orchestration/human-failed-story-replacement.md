# Replacing failed work after a scope decision

An authorized human may retire a failed story through an accepted plan amendment
when its outcome has been deferred or replaced. The state transition is
`failed -> superseded`, permitted only for a human actor. A service actor cannot
use this transition to discard a failure or avoid the retry allowance.

The amendment keeps the original node identity, attempts, failure decisions and
attributed usage. Distinct replacement work uses a new node address and its own
issue contract. The retired story is not passed, and its deferred criteria must
not be reported as accepted. Existing completed work and human evidence decisions
remain subject to the normal amendment preservation checks.

Preview and acceptance use the same transition guard. Existing execution and
claim fences still apply: an active worker, unfinished execution or unsettled
claim must settle before the amendment can cross its plan boundary. The new
transition does not authorize a retry, cancel a live worker, alter budgets or
release an in-flight claim.
