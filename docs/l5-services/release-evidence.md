# L5 release evidence

The shared release publishes two bounded GitHub artifacts for the delivery flow:
`l5-<service>-qualification`, containing `qualification.json`, and
`delivery-evidence`, containing `delivery-evidence.json`. Use hyphens in the
Well-Architected artifact name. Installation receipts and native observations
remain in private storage.

## Service reader contract

Each service's final emitter must independently read the original Product run,
provider results and cleanup. Its version-1 qualification report includes:

- `source_revision`, `definition_revision`, `workflow_path`, `run_id`,
  `run_attempt`, `correlation_id` (or `correlation`) and `contract_digest`, bound
  to the current shared release attempt.
- `epic_criteria`, `story_criteria`, `scenarios` and `live_checks`, each a list of
  records with `id`, the observed `outcome` and `evidence_verified`.
- `observation: independent-live` on native checks. This describes the reader's
  actual verification; a supplied success flag is insufficient.
- `cleanup.outcome` and an explicit `unresolved` list. Missing or unverified
  work must remain incomplete. Non-Security reports also include the accepted
  `requirements_revision`; DevOps includes `portability.assertions`.

The accepted flow defines the exact required IDs. A successful subset does not
establish full acceptance. Preserve separate facts about unavailable provider
signals, external prerequisites and criteria that have not run.

Security writes the report to
`$RUNNER_TEMP/l5-security-qualification/qualification.json`. DevOps writes it to
`$PRIVATE_EVIDENCE_DIR/<authorized-run-id>/l5-devops-qualification/qualification.json`;
the run ID comes from its selected private fixture. Other services write
`$PRIVATE_EVIDENCE_DIR/qualification.json`. These are private emitter outputs,
not directories to upload recursively.

## Shared publication

`modules/L5-services/scripts/publish-release-reports.py` checks the attempt
identity and stages only allowed metadata fields. It replaces private references
and free-form diagnostics with content hashes. Native findings, answers, source
bytes, fixture credentials and browser state are excluded. Original private
reports remain unchanged, with their byte digest retained in the staged report.

An incomplete report may publish diagnostic metadata but cannot publish an
accepted delivery receipt. Artifact upload is conditional on the current staging
step's outputs, so a failed reader cannot upload a leftover file from an earlier
attempt.

For an engine-owned release, the publisher authenticates the exact target through
the release API, verifies the selected AWS principal, reads the immutable worker
manifest and archive, and checks every installed service function against those
bytes. It probes the service entrypoints without creating provider work. A desired
source revision alone is insufficient to establish the installed revision.

The final delivery report uses the Gateway's `WorkflowDeliveryEvidence` model
directly. Its `checks` preserve the independently verified native check results;
the engine still validates the required check IDs, target, workflow identity and
cleanup. Legacy emitter fields such as `required_checks` and
`definition_revision` are not copied into the strict delivery-v2 document.

Standalone operator qualification can publish its bounded report but cannot
claim an engine delivery contract without an authorized flow correlation.
