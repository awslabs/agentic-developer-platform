# V3 fault and integrity qualification

The current report records two defects discovered by qualification: missing immutable prior-attempt history and an HTTP write bound that was tested as a helper but not connected to the deployed route. V3 remains blocked until their fixes are deployed and independently verified.

Native DynamoDB/SQS evidence is executed from the EC2 instance profile against the exact deployed modules. The fixture isolates the recovery discovery shard and private FIFO, journals every owned key before mutation, and cleans them independently even after a failure. It invokes canonical operations directly; this does not claim production recovery scheduling or worker IAM verification. Those boundaries are qualified separately.

To run the current fixture locally without AWS mutations, use `PYTHONPATH=modules/gateway python docs/task-api/evidence/2026-09-25-v3/native_fault_fixture.py --moto` from a source containing PR6029, with test dependencies installed. The operator `--apply` lane requires AWS account879318057152, `WEBHOOK_EVENTS_TABLE=adp-dev-webhook-events`, and `AGENT_AUTHORITY_TABLE=adp-dev-agent-authority`; exact command/provenance accompanies actual output.

Historical cancellation acknowledgement required an explicitly owned DLQ redrive. Later clean clarification completed and released capacity automatically; these outcomes are separated throughout the evidence.
