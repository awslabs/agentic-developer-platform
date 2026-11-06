# Release-readiness qualification and historical recovery

New native release-readiness tasks are blocked with
`release_readiness_provider_contract_unqualified`. The worker retains an
operator-readable explanation in `monitoring.last_poll_error`. This applies to
legacy raw-description inputs too; retaining an old manifest does not authorize
another native submission.

Before enabling new tasks, qualify all of these provider contracts:

- Request and prove the originally selected base/head comparison, including the
  consumed repository, host, revisions and association.
- Prove static-only execution. A request flag or description echo does not prove
  the provider consumed the expected source or avoided automated testing.
- Establish the final `release_analysis_report` schema, finality, citations and
  partial-coverage semantics, and its relationship to generic recommendations.

The documented immutable-head input alone does not establish the comparison
base. ADP's independent base/head snapshots and changed-line hashes support
individual citation checks; they do not prove complete native analysis. No
undocumented provider fields or speculative request version are introduced by
this guard.

Existing effects remain recoverable under current Product/delegation authority.
Reconciliation retains their exact description, reference and idempotency token.
Polling, collection and cancellation use the original SQL submit attempt and
compare its task/execution identity with the run and provider readback. Current
work claims continue to authorize and fence each worker operation. Readiness
cancellation may issue one currently authorized cancellation update for that
existing effect. Cancellation retry observes the same original effect rather
than issuing another update. Verification cancellation retains its existing
unknown outcome; its retry read also uses the original submit attempt.

Collected generic recommendations remain available with their independently
checked citations. The resulting run is `partial`, with `coverage=unknown` and
`coverage_mode=native_observation`, even when every returned recommendation has a
matching citation or the provider reports `COMPLETED`. The qualification reason
remains visible on the run. Historical findings are not discarded or upgraded
into a complete release acceptance.
