# Shared stream quota qualification

The previous implementation enforced stream limits separately in each gateway
replica. The deployed Redis lease implementation now enforces the frozen limits
across replicas. This evidence exercises the two-reader Task limit through two
exact gateway pods and the actual public API, including renewal and release.

Both authenticated direct-pod streams used the existing completed Task and the
same registered external service principal. Owned Kubernetes port forwards
selected two distinct pod UIDs, both running image
`sha256:fddd951f2375102355024db47f5e405ca331bfb991ac3f028ce6871f0a826260`.
Each stream returned its snapshot and four real comment heartbeats. Both stayed
open together for 60.538 seconds, exceeding the initial 45-second lease, before the
third reader was tested. Heartbeats are not counted as authored progress.

The third request used the public API Gateway endpoint. It returned 429 with
`rate_limited` and the exact message, “This task is at its concurrent Task API
stream limit.” A generic gateway rate-limit refusal cannot satisfy the fixture.
Because each selected pod held only one stream, per-pod counters alone could not
have produced this two-reader refusal.

After closing one direct stream, a bounded retry observed public 200 and the
idless snapshot after 1.757 seconds. The expected intermediate 429 is retained.
All three admitted response handles were then closed, and both owned port-forward
processes stopped. A subsequent read-only native Redis audit found zero entries
in the environment, principal and Task sets. No Task state, command, model or
provider operation was created by this fixture.

The direct-pod and public lanes are explicitly distinguished. The separate
private Redis qualification verifies principal/environment limits and lease-loss
cases; this public fixture verifies the Task cap across replicas and renewal
beyond the initial lease. Independent response/route/stream/lease tests passed 94
cases, and the source TCP fixture retained the 10-second blocked-write bound with
all 2049 events recovered. Installed module hashes match the reviewed source.

`parity-global-final.json` binds nine ready gateway replicas and the orchestration
Lambda to the same final image and source
`a16a2a851d17998b8cd51dfa47b77275251229cc`. The worker remains on its separately
qualified 57d image. AI-DLC remains paused. Issue closure waits for code CI/merge
and the independently reviewed V3 report; this evidence does not bypass those
gates.
