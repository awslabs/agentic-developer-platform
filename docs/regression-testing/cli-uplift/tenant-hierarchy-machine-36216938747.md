# Tenant, hierarchy and machine lifecycle live evidence

[Disposable-EC2 run 36216938747](https://github.com/aws-e/adp/actions/runs/36216938747) passed all five selected cases: install, Cognito login/refresh, E27, D03 and D05. The workflow source was `306fe195b9722e92922a43ac98448c38f9d2795a`; the served gateway/CLI source was `b1e266d97a1bf6e4a9c1805a02dd7482b3eafbe0`. The report's separate `harness_commit` identifies the pinned tenant-validation dependency.

- E27 verified concurrent reads in two tenants while changing local defaults and refreshing Cognito credentials. This advances #5622; it does not establish simultaneous inference or lost mutation acknowledgement recovery.
- D03 passed 13 checks covering parentage, ordinary-user read/write refusal, canonical IDs, create/delete retry behavior, stale revisions, populated delete refusal, team membership and foreign parents. A retained signed lease was denied after membership revocation; native-tenant access remained available. Membership was restored and all six owned resources were verified absent. Role-change acceptance for #5623 remains pending.
- D05 passed six canonical-principal checks including duplicate/foreign aliases, stale revision refusal, suspension and retirement. Both owned aliases were revoked and the principal was retired. Ordinary access and session-family readback were preserved. This advances #5624 and the read/denial subset of #5625; it does not establish provider registration, credential issuance or session revocation.

Cleanup completed and EC2 `i-065fe7830571600d2` was independently confirmed terminated. No inference was dispatched. The earlier D05 reporting failure in run 36215959470 remains unchanged; this fresh identity run verifies the #6309 success-marker repair.

The [machine-readable evidence](tenant-hierarchy-machine-36216938747.json) retains the original case details, qualifications and full-report hash. These subsets do not close the remaining story acceptance criteria.
