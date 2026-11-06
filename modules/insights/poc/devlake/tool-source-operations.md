# Optional provider source qualification

This procedure extends the existing DevLake campaign. It does not grant access to
an environment, provision a lab by itself, or qualify a live source from fixture
results. The coordinator and operator select the campaign target, consented
scopes, source-reader credentials, reviewed Kubernetes manifests, headroom and
cleanup authority before using it. Missing GitLab/PagerDuty/Linear/Slack consent
is **unavailable**, not a passing provider observation. Connector readers never
create, post to, or delete provider records.

Linear uses `https://api.linear.app/graphql`. The Gateway sends personal API keys
as the raw `Authorization` value and OAuth credentials with `Bearer`. In the
private `provider_profile_source.py` reader configuration, encode a personal key
as `API-KEY <key>` in `authorization`; the reader removes that configuration prefix
before making the fixed GraphQL query. OAuth reader credentials use `Bearer <token>`.

1. **Select the owned target.** The operator verifies the account and exact
   finalizer role via the selected credential connection, the pinned campaign
   revision, a private reachable cluster, the exact Kubernetes context and its
   SHA-256 cluster fingerprint. Keep the explicit kubeconfig, config, capacity
   measurement and manifests private, outside this repository, mode `0600`.
   Do not switch the machine's current context. Record aggregate *available*
   headroom (at least two CPUs, 8 GiB RAM and 20 GiB disk), its observation time,
   cluster fingerprint and context in the private measurement fields `free_cpu`,
   `free_gib`, `free_disk_gib`, `observed_at`, `cluster_ref` and `context`;
   the measurement expires in one hour. The
   finalizer's protected campaign record must already exist and outlive setup.
2. **Record intent before effects.** Prepare a private JSON config with campaign
   pilot ID, revision, operator, account, region and expiry exactly matching the
   protected ledger; namespace `s14-<pilot-id>`; `cluster_ref` as the SHA-256 of
   compact sorted JSON for the selected kubeconfig cluster entry; the explicit
   context; pinned `repository@sha256:` images for SonarQube, Jenkins and Argo CD;
   and a bounded ordered inventory of 4–24 exact resources (the three `s14-<tool>` Deployments,
   additional named owned resources if needed, and the Namespace last). Include
   seven provider binding entries, each either `null` or a selected `scope_id`,
   `reader_ref` and `consent_ref`; the three disposable tools additionally require
   a distinct `bootstrap_ref`, and GitLab requires an `owner_ref` for the existing
   owned test source. Supply private manifest JSON keyed by
   `Kind/namespace/name`, with matching campaign ownership and revision labels.
   Never put reader tokens, passwords, or Kubernetes Secrets in the ledger or
   these manifests. The fixture bootstrap identity is separate from the
   provider broker's read-only identity. Pin actual compatible images, API and
   plugin versions in the private qualification record; never use `latest`,
   host networking or host mounts (including the Jenkins Docker socket).
3. **Resume only the same run.** In the protected operator workflow, set
   `ADP_DEVLAKE_TOOL_KUBECONFIG` to the explicitly approved private path and run
   `python3 modules/insights/poc/devlake/tool_sources.py --config <private-config> --capacity <private-measurement> --manifests <private-manifests> --setup`.
   The protected ledger records exact intent before any apply. Each creation
   rechecks current ledger authority, expiry and measured headroom; it first
   inspects the exact name, refuses foreign labels or changed image digests,
   then applies and reads back. Repeating setup resumes only its own partial
   creation. A readback without `--setup` inspects owned resources but does not
   claim that a provider API works. Bind separately approved source readers to
   `read_sources` for exact native-scope API smoke and revision evidence; without
   them all provider scopes remain `unavailable` in the output (Kubernetes
   `resource-present` is not an API qualification). For the three owned tools,
   invoke `seed_sources` using only separately approved bootstrap callbacks and
   then verify the selected scope with read-only callbacks. The function never
   bootstraps GitLab or SaaS sources. Record that
   independent private evidence in the one campaign, not in public docs.
4. **Clean up through the existing finalizer.** The scheduled expiry or
   operator-authorized failure/abort finalizer (with the same explicit private
   kubeconfig and scoped cleanup rights) reconciles tool resources before
   retiring its registered ECR images. It checks each exact owned identity and
   label, deletes in inventory order, independently verifies absence and saves
   each receipt to the protected ledger. A failed or lost response resumes from
   the same ledger; it cannot mark the campaign complete while owned resources
   remain. It inventories namespace-scoped kinds before deleting the namespace
   and refuses to delete unrelated content. Existing GitLab and consented SaaS
   resources are **retained**; any desired GitLab deletion needs a separate
   approved owned-source operation. Record the final read/retention receipt or
   explicit unavailable status for each source. The operator owns live SCN-06
   verification and independent cleanup readback.

Offline fixtures: `python3 -m unittest modules.insights.tests.devlake_poc.test_s14_tool_sources modules.insights.tests.devlake_poc.test_s09_cleanup`.
