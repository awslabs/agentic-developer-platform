# V3 fault and integrity qualification

Nine V3 criteria pass; V3-06 remains blocked after a subsequent audit found concurrency caps are enforced per replica instead of across the deployment. Passing write-timeout and TCP evidence is retained with its precise scope. Qualification found and corrected two defects: missing immutable prior-attempt history and an HTTP write bound that was tested as a helper but not connected to the route. The final native rerun passed19checks and cleaned23ownedkeys; installed transport pressure closed the slow client within10.054seconds while useful work completed independently.

Native DynamoDB/SQS evidence is executed from the EC2 instance profile against the exact deployed modules. The fixture isolates the recovery discovery shard and private FIFO, journals every owned key before mutation, and cleans them independently even after a failure. It invokes canonical operations directly; this does not claim production recovery scheduling or worker IAM verification. Those boundaries are qualified separately.

To run the current fixture locally without AWS mutations, use `PYTHONPATH=modules/gateway python docs/task-api/evidence/2026-09-25-v3/native_fault_fixture.py --moto` from a source containing PR6029, with test dependencies installed. The operator `--apply` lane requires AWS account879318057152, `WEBHOOK_EVENTS_TABLE=adp-dev-webhook-events`, and `AGENT_AUTHORITY_TABLE=adp-dev-agent-authority`; exact command/provenance accompanies actual output.

Historical cancellation acknowledgement required an explicitly owned DLQ redrive. Later clean clarification completed and released capacity automatically; these outcomes are separated throughout the evidence.
