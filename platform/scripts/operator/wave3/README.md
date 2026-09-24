# Protected task acknowledgement evidence

`collect_ack_receipt.py` reads the durable task-delivery tombstone with the
operator's existing AWS credentials. Supply `--ledger`, `--identity`,
`--dispatch-intent`, `--table`, `--region`, and a fresh `--out` path. It never
publishes, receives or deletes an SQS message, or mutates a DynamoDB row.

The gateway retains the SQS message ID, receipt hash, response request ID,
HTTP status, SDK retry count, reserved acknowledgement-attempt count and time.
It removes the task body, queue URL and reusable receipt from the tombstone.
Only a matching published message and invocation, one reservation, zero SDK
retries and a successful AWS response establish a single clean acknowledgement.
A crash before calling SQS can increment the reservation count without a call;
multiple reservations are ambiguous and are refused by the collector. Missing
metadata on older records is also refused, never replaced with guessed values.

This receipt is one part of Wave 3 acceptance. Capture the worker's actual exit
before Job TTL cleanup and independently observe no redelivery over a window
longer than the queue visibility timeout. Neither a terminal invocation row nor
an empty approximate queue count substitutes for these observations.
