# Retire duplicate expired queue deliveries

After recorded original-planning recovery, old SQS copies may delay the fresh
invocation. `expired_delivery.maintain` is a bounded installation-owner entrypoint.
Hold the original installation lock, pause API dispatch and KEDA launches, wait
for all worker pods to finish, and verify the installed Gateway recovery source
and image before invoking it with that Gateway's `recovered_delivery` reader.

The reader independently checks the immutable retry proof, original identity,
approval, plan, grant expiry and current lease. Callers cannot supply absence
claims. Only exact proven envelopes are acknowledged; fresh, unrelated and
unproven deliveries are released. There is no queue purge or execution reset.
Every deletion has a conditional immutable intent in the protected provider
journal before SQS acknowledgement and a separate acknowledgement receipt after.
Lost responses retain the intent. A failed visibility release is attempted for
all remaining messages before reporting the error; SQS also expires their leases.

The selected infrastructure owner must match the protected provider enrollment.
The installer checks paused workloads and holds the installation lock for the
whole bounded run (400 messages / 120 seconds). Restore the recorded dispatch
and KEDA settings through that same owner after successful maintenance. Inspect
retained receipts before retrying any failed run; never delete unproven messages
to empty the queue. This operation does not authorize execution, spend or recovery.

When unproven old copies repeatedly block the current invocation, the owner may
select `retained_visibility_seconds` from 0 to 300 (default 0). This is only a
finite SQS visibility lease on retained copies of the exact selected original
operation. Unrelated/malformed messages return immediately. No message is deleted
and no expired envelope can execute. Restoring API dispatch publishes the same
protected current invocation, which can reach a worker during that short lease;
retained copies automatically become visible again afterward. The installation
receipt records the selected delay. Keep workers and API dispatch paused during
maintenance and restore both promptly after the bounded run.
