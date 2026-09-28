# T4 and T8 implementation acceptance review

All five T4 criteria and all five T8 criteria have implementation evidence in [criterion-review.json](criterion-review.json). Recommend closing implementation stories #5797 and #5801 after independent root review.

T4 combines 184 focused host/dispatch/client/seam regressions, the clean public completion and actual-image tests. The remaining legacy-fixture gap was exercised against installed worker image `57d938…`: 26 tests cover full Claude and Codex entrypoint execution fixtures, Codex raw-envelope stdin/captured result handling, and dedicated review finalization. External process/API responses are controlled in that lane; separate live legacy runs remain coexistence evidence. PR #6016 owns those immutable image artifacts.

T8 combines 20 passing external-client/readiness/packaging tests, working no-GitHub public invocation and verified artifact retrieval, read-only compatibility guards, and ordered rollout/rollback documentation. The captured [admission-off snapshot](admission-off-snapshot.json) demonstrates that accepted-task reads remained available while new submission was disabled.

This implementation review does not close V3/V4/V5 or claim native capacity settlement, held-input coexistence, or revocation qualification has completed. The T3 capacity fix and independent qualification owners retain those responsibilities. The earlier failed diagnostic tasks remain failures; the clean task completion used no operator state repair.
