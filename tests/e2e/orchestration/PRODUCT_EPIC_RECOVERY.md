# Product epic inventory recovery — procedure v1

This is a **manual, live** procedure for an already authorized Product-bound
qualification. It does not authorize a paid run, deployment or live acceptance.
Ordinary PR CI only executes offline tests. The operator who owns the fixtures
must review the private config, confirm the Product, planning epic and both
change-repository connections, and stay within the approved spend, attempts,
concurrency, deadline and retention limits. Keep credential values and provider
identities in the private config/evidence, not in this document or dispatch inputs.

1. Record the reviewed Git ref and commit, configuration path, environment,
   qualification ID from the original run, and that run's GitHub Actions run ID.
   Keep the config and manifest pinned to the approved revisions. In the private
   config select only `product-epic-recovery` with the **same** `product_epic`,
   registered targets, artifact directory and bounds as the original run.
   The private reviewed scenario manifest must pin separate API Deployment and
   client completed-Job scope and distinct deployment workflow paths. For the
   client runtime set `kind=job` and omit `deployment`: the producer creates
   `client-{run_id}-{run_attempt}`, not a fixed Job name. The harness selects one
   completed successful run of the reviewed client workflow at the story merge
   revision through authenticated provider reads, then reads that exact Job in
   the reviewed cluster/namespace. Missing or ambiguous workflow matches, changed
   source/run/attempt attribution, foreign Pod owners and changed Job/Pod UIDs
   refuse qualification. Keep the inventory from the original run; do not edit
   its evidence to substitute a newer Job. The
   installed deployment manifest must already contain approved, source-pinned
   entries for both components. A missing entry is a refusal, not a reason to
   enable an unknown target or re-label a Gateway image as source evidence. Do not
   switch to a different Product, repository or environment to make cleanup pass.
   If the original inventory artifact has expired or the prior run's checkout
   cannot be verified, stop and escalate to the fixture owner; do not start a
   replacement qualification to recreate it.
   Product story fixtures keep root `app.py` and `client.py` entrypoints and their
   existing commands. Their component-specific Dockerfiles retain the original
   image recipe and copy the qualification packages alongside those root files;
   the existing deployment workflows build those Dockerfiles from repository
   root. Any changed workflow definition still requires reviewed source pins
   before dispatch; the harness does not update deployment authority. The client
   contains its own pricing module, never an import from the API
   repository. Offline checks exercise that layout with synthetic source files;
   they do not establish that a live image has been built or deployed.
2. In the manual **Orchestration Live Tests** workflow at the reviewed ref,
   choose the matching `environment`, the reviewed repo-relative `config_path`,
   `mode=preflight`, and leave both IDs empty. Inspect the read-only report:
   verified Product and targets, `scenarios=[product-epic-recovery]`,
   `allowed_modes=[resume,cleanup]`, and no planned new fixtures. A refusal
   needs a prerequisite fix and another preflight, never a blind retry.
3. Dispatch `mode=resume` with the same config and environment. Set
   `qualification_id` to the recorded qualification and `source_run_id` to the
   **latest run that uploaded its inventory** (initially the original run).
   The workflow downloads that run's artifact, verifies its version, owner,
   qualification and environment, then reconciles through provider reads and
   native-parent checks. Inspect `attempt_history`, `native_parent_verified`,
   `unresolved` and `reconcile_failed` in the report. An incomplete or failed
   outcome is not acceptance; preserve its artifact for investigation.
4. For cleanup, dispatch `mode=cleanup` with the same qualification ID, config
   and environment, but set `source_run_id` to the **resume run ID** (or the
   original run ID if resume was intentionally skipped). Each attempt writes an
   updated inventory. If cleanup is interrupted or refused, use that cleanup
   attempt's run ID for the next cleanup/resume. Never go back to an older run:
   doing so loses reconciliation and deletion progress. Do not dispatch two
   recoveries of the same qualification concurrently.
5. Inspect `deleted`, `retained_audit`, `refused`, `failed` and
   `unresolved_inventory` in the cleanup report and retain the latest artifact.
   Provider-verified owned resources alone may be removed; the Product and
   planning epic are not disposable fixtures. Nonzero exit, refused ownership,
   unresolved intent or retained live resources means cleanup is **incomplete**.
   Report each remaining resource and its recorded reason to the fixture owner
   for bounded reconciliation before retention expires; do not assert live
   acceptance from a successful preflight or offline test.

The workflow is dispatch-only; `source_run_id` is an Actions run number, **not**
the qualification ID. It is required on both resume and cleanup because each
dispatch starts with an empty workspace. These inputs are identifiers, never
credentials. The reviewed config example in this repository is deliberately
non-runnable and must not be used as live approval or target authority.
