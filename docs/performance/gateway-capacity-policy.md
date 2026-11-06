# Gateway capacity selection and ownership

This is a deployment mechanism, not a qualified capacity policy. Both maintained gateway deployment paths read `/adp/<environment>/gateway/capacity-profile`; an absent parameter selects `default`, and only `default` or `llm-proxy` is accepted. A failed SSM read, invalid selection or failed database budget stops the rollout. The gateway HTTP profile does not change the scheduled Lambda's separate connection configuration. Do not select the CPU comparison profile until its image, connection reserve, terminating-pod overlap, failure behavior and workload comparison have been approved against a real target.

The opt-in profile needs four non-negative SSM parameters in the selected environment: `capacity-db-max`, `capacity-db-reserved`, `capacity-db-other` and `capacity-terminating-pods`. The operator records a complete fleet worksheet covering 12 HPA replicas, one opt-in surge pod, terminating old pods, other consumers (including scheduled jobs, callbacks, migration and administrators) and reserved slots. The renderer rejects an opt-in selection unless `20 × (12 + 1 + terminating pods) + reserved + other ≤ db-max`. The default profile is 12 HTTP connections per pod with a 50% rollout surge; no source setting establishes live database headroom. The larger split-pool canary remains disabled regardless of profile.

Per-worker inference protection is **opt-in** until load comparison selects values. All three SSM fields must be present together: `capacity-max-active-streams-per-worker` (1–100,000), `capacity-stream-admission-queue-limit` (0–10,000) and `capacity-stream-admission-wait-ms` (1–5,000). An absent set preserves the current admission behavior; a partial or invalid set rejects rendering instead of publishing an unreviewed default. These are safety bounds on explicit inputs, **not** recommended capacity values. When configured, each gateway HTTP worker admits at most the chosen concurrent model requests, waits at most the chosen milliseconds for one of the chosen queue slots, and rejects saturation before model dispatch with a retry hint. Health, administrative routes and scheduled Lambda are unaffected; tenant quotas still apply separately. Active streams retain their permit through terminal or cancellation, and an error releases it. Finite safe values and connection headroom must be qualified against the model/API/workload matrix before live activation.

## Declared resources and cooldown (source configuration, not live evidence)

Both profiles retain HPA `minReplicas: 2`, `maxReplicas: 12`, a one-pod disruption budget, and a Deployment rollout that does not remove a pod until its replacement is ready. The Deployment's two replicas establish first-install baseline only: updates preserve the current replica count and HPA owns subsequent scaling. The renderer refuses a missing resource request/limit, disabled or unbounded downscale policy, or a disruption budget that could remove both baseline pods. Existing settings are:

| HTTP profile | CPU request / limit | Memory request / limit | Scale-out signal | Normal HPA downscale |
|---|---|---|---|---|
| `default` | 250m / 1000m | 2Gi / 3Gi | memory 75% of request, CPU 70% backup | stabilize 600s; at most one pod every 120s |
| `llm-proxy` comparison | 1 / 2 CPUs | 1Gi / 2Gi | CPU 1000m average per pod | stabilize 120s; at most one pod every 60s |

The default memory floor addresses a confirmed pricing-verification OOM on
2026-10-22: four-worker serving pods used approximately 1.3–1.9Gi before a
migration/probe exec exceeded the former 2Gi limit. The maintained renderer
raises older default manifests to a 2Gi request and 3Gi limit, preserving larger
settings. This also covers workflow dispatches that deliberately deploy an older
reviewed source. At the 12-replica ceiling, the six surge pods now request 12Gi
in total, plus existing serving and terminating pods. The HPA memory threshold
becomes 1.5Gi; these active-traffic observations do not establish idle memory,
final load capacity, or cooldown acceptance. Pricing checks remain mandatory.

These are configuration values, not recommended final settings or an OOM/success receipt. Starting at 12 replicas, if stabilization starts when admissions stop and scale-down remains eligible, ten one-pod steps require approximately 600 + 9 × 120 = 1,680 seconds for `default`, or 120 + 9 × 60 = 660 seconds for `llm-proxy`; lingering memory pressure, unavailable metrics, or drain can extend this. The campaign's separate 600-second cooldown observation cannot guarantee a two-pod return from 12 with either source profile. At the deadline record the actual desired/current/ready timeline, health, OOM/restarts and unchanged configuration as **incomplete** if two ready baseline pods have not returned. Do not shorten the HPA window, manually force replicas, or claim acceptance to meet a clock. A measured final cooldown policy needs the live owner and approved comparison.

## Observe natural cooldown without scaling the fleet

This is an **operator procedure**, not a live acceptance claim. First verify the private target, exact account and role, cluster context, deployed image, selected profile, HPA/pool settings and baseline health against the reviewed artifact. Use a private owner-only directory (`PRIVATE_DIR`) for all inputs and receipts. Before stopping admissions, snapshot the three controlled objects. `VERIFIED_CONTEXT` is the context whose target and identity the operator has just checked; it is not inferred from the script:

```bash
umask 077
kubectl --context "$VERIFIED_CONTEXT" -n adp-gateway get deployment/bedrockgateway hpa/bedrockgateway pdb/bedrockgateway-pdb -o json > "$PRIVATE_DIR/before.json"
```

Stop **new** campaign/controller permits across every worker using the authorized campaign procedure, record the actual UTC stop time in `ADMISSIONS_STOPPED_AT`, then drain existing streams and reconcile accounting within the separate 240-second drain limit. Do not stop pods, change HPA targets or force replicas down. Start the existing read-only sampler immediately after stop (in a separate terminal while drains proceed if needed); its eleven one-minute samples bound the observation to a nominal 600 seconds. The sampler can overrun if Kubernetes reads are slow: a late sample does not retroactively establish return before the stop-plus-600-second deadline.

```bash
umask 077
python3 platform/scripts/gateway-capacity-report.py --output "$PRIVATE_DIR/cooldown.jsonl" --samples 11 --interval 60
kubectl --context "$VERIFIED_CONTEXT" -n adp-gateway get deployment/bedrockgateway hpa/bedrockgateway pdb/bedrockgateway-pdb -o json > "$PRIVATE_DIR/after.json"
python3 platform/scripts/gateway-capacity-cooldown.py --samples "$PRIVATE_DIR/cooldown.jsonl" --before "$PRIVATE_DIR/before.json" --after "$PRIVATE_DIR/after.json" --admissions-stopped-at "$ADMISSIONS_STOPPED_AT" --deadline-seconds 600 --output "$PRIVATE_DIR/cooldown-receipt.json"
```

The evaluator never contacts AWS or Kubernetes, never overwrites a receipt and reports only timestamped desired/current/ready replica counts, metrics availability, restart/endpoint observations and **names** of changed configuration fields; the raw manifests remain private. It ignores Deployment `spec.replicas` because the HPA owns it, but compares the pod template, rollout strategy, HPA/PDB spec and object UIDs. A `candidate` needs two consecutive fully ready/serving two-pod observations within the deadline, available metrics, no observed or unknown restarts, and unchanged configuration. An unmet condition is `incomplete`, not a pass. Neither the stop-admissions attestation nor absence of manual scaling, OOMs between samples, stream drops, accounting cleanup or live health is provable from the timeline; retain independent private controller, health and cleanup receipts. The named live-validation owner binds all evidence to the revision and attempt; source checks and a synthetic `candidate` do not satisfy AC-03 or SCN-02 live acceptance. Never publish raw private receipts or target identity.

## Selecting a profile

After verifying the *exact* private target account, role, cluster, region and SSM parameter version, an authorized operator uses `platform/scripts/gateway-capacity-lock.py select`. Set the placeholder variables to reviewed values in the selected terminal, and use an owner-only directory for `OWNER_FILE`. `EXPECTED_VERSION` is the version read immediately before selection, or `0` only when `ParameterNotFound` was independently confirmed. Do not infer version `0` from a permission failure or timeout.

```bash
python3 platform/scripts/gateway-capacity-lock.py select \
  --account "$ACCOUNT" --region "$REGION" --environment "$ENVIRONMENT" \
  --cluster "$CLUSTER" --namespace "$NAMESPACE" \
  --profile "$PROFILE" --expected-version "$EXPECTED_VERSION" \
  --owner-file "$OWNER_FILE"
```

The selection command acquires the same DynamoDB `adp-terraform-locks` target key used by deployment and rollback, checks the expected SSM version, writes the selected value and confirms the next version. A competing owner, changed version or invalid profile is rejected rather than overwriting a newer selection. A failed or ambiguous write retains its owner file and lock for reconciliation; never retry with a fabricated version. To restore an earlier **compatible** image and capacity settings, select its approved profile at the *current* version using the same command, then reapply its reviewed artifact through the protected deployment path. Do not bypass the lock with a direct SSM edit or a raw `kubectl apply`. Source/merge success is not live acceptance.

## Recovering a stopped owner

The launcher retains a private rendered bundle beside its target-bound deployment checkpoint, in a directory accessible only to its owner. When `--resume` or `--from` skips a completed gateway phase, it reacquires capacity ownership and verifies the saved selection and deployed image/resources before continuing later phases. It does not rebuild the gateway. Missing saved files, changed selection or a different deployed configuration stop the resume rather than restoring stale settings. After reconciling the target, use `--from gateway` with the same reviewed source and options to render and apply again. Keep the checkpoint and its capacity directory private; deleting them removes this resume capability. Final verification still checks the Deployment, HPA and PDB together.

Ownership has no automatic expiry: a slow migration or paused runner cannot lose ownership merely because a timer elapsed. If a runner dies, the authorized operator must establish that its process stopped, retain a private receipt binding the exact lock target and owner, and snapshot the current Deployment, HPA and PDB UID/resourceVersion (record `null` when a resource is truly absent). The receipt includes `target`, `owner`, `started`, `stopped: true` and `resources` with `deployment`, `hpa`, `pdb` keys. The program re-reads all three objects and deletes the lock **conditionally on the same owner and start time**. It refuses a changed bundle, an unreadable object, a newer owner, or a receipt that does not attest a stopped owner. The stop attestation is operator-provided; this CLI cannot independently prove a process terminated. The receipt, private target identity and failure evidence must remain in the protected operator store, never this repository.

```bash
python3 platform/scripts/gateway-capacity-lock.py recover \
  --account "$ACCOUNT" --region "$REGION" --environment "$ENVIRONMENT" \
  --cluster "$CLUSTER" --namespace "$NAMESPACE" \
  --receipt "$PRIVATE_STOPPED_OWNER_RECEIPT"
```

Recovery only releases a stale lock; it never rolls back configuration or deletes metering/accounting records. Re-read the selected SSM version, Kubernetes resource versions and budget before a retry. The profile renderer checks selection around migration/apply and reads back the deployed image, profile, resources, HPA and PDB after rollout; changed state is reported as incomplete, not assumed safe. The operator handles any interrupted or partly applied rollout on its actual target. A private target, approved SLO and the #7031/#7032/#7035/#7036 prerequisites are required before a live policy claim.
