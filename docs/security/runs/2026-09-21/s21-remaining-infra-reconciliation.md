# Remaining infrastructure observations — #6109

Status: **open**. This repair preserves the assigned371 original selectors:
367 Checkov findings and4 CloudFormation warnings. The immutable source manifest
SHA256 is `f781861a5b573c1b08a03117fa5d4a5ae0f8602625f4b6e867e626e967251f62`.
The companion JSON retains each original artifact/run/result identity and historical
disposition candidates. It replaces the PR's erroneous462-row substitute inventory
and unsupported287 design acceptances. Historical risk decisions are not renewed.

Three bounded source controls are implemented:

- `ri35`: enable point-in-time recovery for the durable Beads DynamoDB manifest.
- `ri69` and `ri70`: explicitly enable SQS-managed server-side encryption on both
  agent-submit FIFO and dead-letter queues. This does not introduce a KMS key or
  change producers/consumers' IAM permissions.

A separate Checkov3.2.346 scan of those two files reports3 passes,0 failures and0
parsing errors for CKV_AWS_28/CKV_AWS_27. The JSON records its raw result hash and
exact source-file hashes separately from the original final scanner run. It is
not a rescan or clearance of all371 findings. The3 rows remain
`source-control-pass-live-open` until the owning deployment plan/apply and live
attributes establish convergence. PITR enables ongoing backup charges; the
manifest contains durable issue state rather than an expiring cache/token table.

The remaining368 original observations remain explicitly open for resource-specific
review and remediation. No blanket risk acceptance, fictional approver, or umbrella
epic deferral is counted as implementation. The4 CloudFormation warnings are
retained individually. Findings outside this issue's frozen scope remain with their
existing named owners; no shared-state or gateway/tick live write was performed.
