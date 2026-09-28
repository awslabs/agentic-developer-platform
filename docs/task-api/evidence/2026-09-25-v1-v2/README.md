# V1 independent ingress and persistence review

V1 #5802: all nine criteria PASS. The criterion report binds the exact source revision, commands, test counts and evidence limitations. Four suites executed 845 tests with no failures; five real RS256 boundary cases additionally exercised the production Cognito validator with a controlled JWKS fetch. Sanitized live identity/revocation evidence accompanies the report; real task storage, concurrency, IAM denial and artifact integrity evidence is linked to the previously reviewed T1 directory.

V2 #5803: all eight criteria PASS in the separate V2 criterion report. The additional21 ingress isolation tests cover GitHub, agent-trigger and EventBridge paths with Task admission off/on and task module import failure. Actual image57d execution/finalization fixtures passed26 tests; controlled image fixtures are distinguished from the separately merged clean useful livecompletion. The failed initial legacy test harness dependency setup is retained.

No token or secret is included. AWS TTL cleanup is asynchronous; these results do not promise a deletion timestamp. IAM simulation of artifact permissions is distinguished from actual DynamoDB denied calls and actual artifact integrity checks.
