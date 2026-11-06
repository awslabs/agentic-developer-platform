# Observe a failed bootstrap while paid workers remain paused

The normal dispatcher and ScaledJob share admission activation. After a bootstrap
worker stops, keeping them paused also prevents a native recovery observer from
closing its expired lease. The bounded path below publishes one recovery task and
runs one observer Job without enabling either paid admission or the ScaledJob.
It uses the ordinary Gateway workload bootstrap, fresh recovery scope and
`recover_lifecycle`; it never imports an expired worker credential or edits the
operation projection directly.

Install compatible Gateway, API and worker sources through normal review, CI,
image qualification and saved runtime maintenance. Gateway must understand the
paired `expected_version`/`expected_fence` dispatch guards, and the worker image
must contain `superplane_executor.observation_only`. Keep the current worker
binding consistent in the API and every Gateway process. A source checkout is
not an installed-image qualification.

1. Observe the original worker termination, operation version and expired fence.
   Through the ordinary authenticated workspace API, the original human requester
   posts `/workspaces/{workspace_id}/bootstrap-recovery/observe` with those exact
   `expected_version` and `expected_fence`. Gateway's original deterministic
   dispatch identity is the idempotency boundary. Only
   an existing bootstrap admission can be selected. The producer verifies its
   domain registration and retained budget; Gateway rechecks the original humans
   and fence under its existing operation lock. This is an observation request,
   not a new paid decision. Dispatch remains disabled.
2. Retain the exact native response privately. A lost response requires inspection
   of the original deterministic dispatch mapping; do not publish a different
   task. This does not acknowledge or remove any queue message.
3. Under the existing Installer owner lock, use
   `installation.bootstrap_observer.plan` with that response, a new 24-hex-character
   attempt ID, and the reviewed image/source qualification SHA256. Review the
   exact Job before `apply`. It derives from the installed paused worker,
   preserves its image, entrypoint, role, network, policy and database mounts,
   mounts retained lifecycle state read-only, and adds an exact recovery-only
   selector. It has one pod, no retry and a ten-minute maximum lifetime. It
   changes neither the API nor ScaledJob, and does not join the API Service.
   If the paused queue also contains another operation's deliveries, the native
   `expired_delivery.maintain` selection may defer at most four exact recorded
   body SHA-256 values from the same domain organization for at most 300 seconds.
   This option requires `immediate_invocation_id` and is retained in its owner
   receipt. It preserves those messages and every operation/grant; the original
   operation identity still exclusively controls acknowledgement proof/deletion.
   Check a complete bounded queue census before creating the one observer Job.
   An incomplete census requires another reviewed finite maintenance pass, not
   an observer restart or an unproved queue deletion. Visibility expires normally;
   no message or authority is recreated during restoration.
4. The worker rejects execution or foreign messages before requesting a run
   credential. It repeats that check before entering the authenticated recovery
   branch. A rejected message remains subject to ordinary delivery expiry; it is
   never treated as obsolete or acknowledged. Investigate such a result before
   preparing another bounded observer. No recovery session can call `/lease` or
   the paid provider-session route.
5. Read the native recovery claim, call disposition, accounting, settlement
   delivery and closed lease independently. An unknown result retains the budget
   and all original journals. A completed Job alone proves neither a usable
   workspace nor eligibility for later cleanup/retry. No attempt is added and no
   original Terraform apply or `/resume` is repeated.
6. `observe` retains the original Job/Pod UIDs and terminal state. `teardown`
   removes only that terminal Job with UID/resourceVersion preconditions and
   foreground pod deletion. `verify_absence` independently proves both absent,
   including after a lost deletion response; repeat absence reads, never an
   uncertain delete. Retain the native settlement and evidence. A lost create
   remains unresolved until its exact identity is independently established;
   `apply` cannot replay it. Preserve the ordinary owner-lock recovery protocol.

Fresh original-source/expiry/grant cleanup qualification and the original human's
native recovery decision remain separate gates before any later paid execution.
The observer needs the existing producer dispatch and worker recovery grants;
it introduces no IAM policy, Kubernetes administrator grant or provider session
export.
