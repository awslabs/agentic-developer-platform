# Six-hour Task deadlines

The Task service-policy ceiling is **360 minutes (six hours)**. This replaces the
original 30-minute pilot ceiling, authorized by the project owner. The deadline
is fixed at acceptance: `accepted_at + max_duration_minutes`. Queue time,
analysis/tool waits and clarification waits all count toward that deadline.

An administrator sets `limits.max_duration_minutes` to `360` in the principal's
versioned Task service policy to use the full duration. Lower policy limits remain
valid. Existing policies and already-accepted deadlines are not silently widened;
policy updates apply to subsequently submitted Tasks. The public submit body
continues to contain task content, not authority or deadline overrides.

## Credentials, monitoring and stop behavior

A longer task does not require a six-hour bearer credential. Worker run credentials
retain their short maximum 15-minute lifetime. The trusted Task host renews them
using its same pod-bound assignment and validates Task/run/generation/persona and
deadline continuity. Renewal rechecks platform authority, cannot change the
Task deadline, and does not replay a model or tool operation.

Client OAuth tokens have their own lifetime. Clients obtain fresh tokens for
status, SSE reconnection, input and cancellation. The accepted Task is not tied
to the original caller access token remaining unexpired. Queued input commands
still must be consumed before their own authorizing token expires.

A progress-stream connection may end before the Task; reconnect with the last
successfully handled event cursor. A lost/expired stream is not a Task failure.
Status and retained events remain readable after the Task deadline, subject to
normal ownership, authorization and retention.

Cancellation/control reads and settlement remain available using verified
pod-bound stop authority when the normal run credential cannot renew. This does
not authorize further model calls, new tool work, input consumption or successful
finalization. At the Task deadline the host stops execution and reconciles exit
and downstream-job evidence. Unknown stop outcomes remain explicitly unresolved.

## Independent limits

Duration does not increase model turns, per-call output tokens, spend, tool
operation counts or concurrency. Those limits remain independently enforced.
A task can therefore finish or exhaust another limit before six hours.

The cyber tool Lambda continues to handle short submit/status/cleanup requests;
backend analysis jobs may run independently. This change does not convert that
Lambda to a durable function or hold an HTTP request open for six hours.

## Rollout

Deploy the compatible gateway and worker versions before raising principal
policies. The shared worker Job default is 22,200 seconds: six hours plus ten
minutes for startup/cleanup overhead. Existing explicit infrastructure overrides
must also cover the remaining Task deadline plus stop/cleanup overhead. The
Task itself still gets at most six hours. Projected workload tokens rotate through
Kubernetes; callers reread them. SQS visibility is renewed by heartbeat rather
than held for six hours. Keep recovery enabled.

Use the existing version-checked Task policy administration API to change only
`limits.max_duration_minutes` while preserving persona/tool permissions, budget,
turn limits and model-policy version. A lower limit chosen for another principal
must not be raised as a side effect of this rollout.

Clock-driven tests verify six-hour admission, short-credential renewal beyond
15 minutes, deadline-capped credentials near hour six, and refusal after expiry.
These are lifecycle tests, not evidence of a six-hour live backend soak test.
