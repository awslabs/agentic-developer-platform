# V3 fault and integrity qualification

All ten V3 criteria pass with explicitly separated deterministic, native AWS, installed-image TCP and public HTTP evidence. Qualification exposed and corrected missing prior-attempt history, an unapplied blocked-write timeout, and per-replica stream caps. Final native DynamoDB/SQS checks passed 19 checks with 23 owned keys removed. Native Redis passed 16 checks with 93 private keys removed; two real gateway pods and the public API confirmed the shared stream limit and renewal. The final installed transport fixture closed a slow client in 10.026 seconds while useful work completed independently.

Native DynamoDB/SQS evidence is executed from the EC2 instance profile against the exact deployed modules. The fixture isolates the recovery discovery shard and private FIFO, journals every owned key before mutation, and cleans them independently even after a failure. It invokes canonical operations directly; this does not claim production recovery scheduling or worker IAM verification. Those boundaries are qualified separately.

To run the current fixture locally without AWS mutations, use `PYTHONPATH=modules/gateway python docs/task-api/evidence/2026-09-25-v3/native_fault_fixture.py --moto` from a source containing PR6029, with test dependencies installed. The operator `--apply` lane requires AWS account000000000101, `WEBHOOK_EVENTS_TABLE=adp-dev-webhook-events`, and `AGENT_AUTHORITY_TABLE=adp-dev-agent-authority`; exact command/provenance accompanies actual output.

Historical cancellation acknowledgement required an explicitly owned DLQ redrive. Later clean clarification completed and released capacity automatically; these outcomes are separated throughout the evidence.
