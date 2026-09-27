# Review startup and model-policy inheritance

Review continuations transfer the developer's held claim rather than entering
ordinary pending admission. They attach the parent's immutable model-policy
snapshot after protected provisioning and before SQS publication. Runtime posture
still decides whether the worker can execute; the engine does not select a model.

An initial enforcing `snapshot_missing` refusal records a protected failed
startup and an activity error atomically, before sending any credential. It does
not claim worker success or model execution. Successful bootstrap records
credential issuance first; that marker and control registration fence subsequent
refusal/recovery. An ambiguous database write withholds credentials.

The review controller can reserve a successor for this specific recorded failure.
The failed action remains counted against its stage. Development attempts and
claim generation do not change; ordinary policy, PR identity, head, authority and
per-stage admission checks still apply. A repair startup retry retains its
findings and author. No general failed worker is treated as completed successfully.
The generic claim sweeper leaves this failure to the review controller so it
cannot release the claim before retry.

A failed startup without a snapshot is traversed only through its protected
parent lineage to inherit the same immutable ancestor snapshot. Missing, cyclic
or overly deep ancestry remains unavailable; recovery never invents model settings
on the failed invocation.

For historical failures before this marker existed, an operator may call
`bootstrap_failure.record_refusal` only after verifying the retained gateway
refusal request ID, timestamp, invocation and exact pod binding. This records the
observed failure using the same fenced transaction. It cannot overwrite a
snapshot, control registration or credential-issuance marker and does not reset
attempts, claims, grants or workload bindings. Preserve the gateway log reference
in deployment evidence. Queueing the successor remains the engine's responsibility.

New snapshots use each persona's registered SDK revision, rather than stamping
Claude's revision onto Codex. At runtime, the known legacy Codex stamp can be
interpreted using the current registered Codex contract after live class posture
is verified. An explicit frozen model can be resolved without a historical class
default that did not yet exist; absent historical posture remains null evidence.
The snapshot and its digest remain immutable, no default model is substituted,
and the current model-permission and invocability checks still apply.
