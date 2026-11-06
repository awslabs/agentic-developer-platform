# Parallel implementation and grouped deployment

The dependency graph is in delivery-stages.json. Its topological levels are a review aid, not mandatory global waves: dispatch a story as soon as its own prerequisites are satisfied. Do not make an unrelated provider or a long shared Gateway workflow a barrier for independent source work.

| Workstream | Independent work after its contracts exist | Shared writer rule |
| --- | --- | --- |
| Domain contracts and persistence | C01–C03, later T01 | One schema owner; C02 → C03 → T01. Provider adapters consume these schemas rather than adding competing migrations. |
| Execution and authority | C04–C05, later K02–K03 | Preserve the existing shared execution owner. Rebase on reviewed tactical-recovery changes before touching the same interfaces. |
| Publication and management Argo | C07, W01–W04 | C07 owns publication intent; W02 owns target registration; W04 consumes both and owns readiness integration. |
| AWS account and networking | A01–A02, N01–N03 | Account and network adapters use distinct modules. One Terraform resource owner and state lock per target. |
| Cluster/bootstrap | K01–K03 | Infrastructure adapter, grant lifecycle and component lifecycle are separate stories with explicit handoffs. |
| User experience | U01 and U02; later T10, T11, T15 | UI and CLI can run together. T10 → T15 serializes shared training UI work. Backend-derived fixtures are mandatory. |
| Lifecycle and migration | L01–L02, M01–M04 | M01 is observation/shadow mapping; M02 changes ownership only under a fence. M03 restore and M04 upgrade stay separate. |
| Training and capacity | T01–T15 | Planning, approval, job dispatch, observation, cancellation, continuation and capacity have separate owners. Argo application ownership must not include individual TrainJobs. |
| Azure, GCP and Nebius | P01–P14 | Separate provider modules and profiles; no concurrent edits to common schemas. Each provider's recipe and controller adapter are separate medium units. |
| Release/verification | R01–R04, V01–V07 | One writer to a shared installation at a time. Verification stories reuse installed artifacts. Provider fixtures can be qualified independently. |

Every source story is sized Medium and has one principal outcome. The graph deliberately separates cancellation from checkpoint continuation, grant recovery from component recovery, and provider infrastructure recipes from execution adapters. A finding that introduces another independently testable outcome should produce a new bounded unit rather than silently expand one of these.

Implementation can use several concurrent agents according to the accepted delivery policy. This planning document does not authorize a concurrency or spend limit. The coordinator should allocate capacity across independent workstreams and avoid multiple agents modifying the same shared schema or migration chain.

Three Superplane batch releases are planned. R01 establishes the AWS/core platform; R02 adds training; R03 adds other providers. R2 and R3 source work can overlap. Their deployment order is serialized only where the actual shared release state demands it. V01/V07 do not each redeploy R1; V03/V04/V05 do not each redeploy R3. R04 adopts the repaired workspace with qualified R1 artifacts and does not require another image build.

Gateway continues its normal automatic main-branch builds and releases. The tactical repair and strategic C08/R01 work must inspect actual serving replicas and scheduler compatibility and preserve unrelated concurrent changes. Do not disable or hold shared Gateway deployment to manufacture a stable baseline.

The earliest operational result is a qualified AWS workspace and adoption capability. New-account and other-provider live fixtures have independent acceptance owners. The requested 2–3-day period is a delivery target whose feasibility must be reassessed against the accepted graph, CI capacity and actual provider prerequisites; it is not a promise that closed source stories imply deployed end-state acceptance.
