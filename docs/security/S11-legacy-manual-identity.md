# Legacy manual identity provenance

Historical `admin_manual` labels are ambiguous: both authenticated administration
and automatic channel placement wrote them. A timestamp or a non-shadow User
cannot distinguish the two. The current gateway and webhook therefore treat
`admin_manual` as routing information only, never as proof for workspace linking,
spend/person identity, protected approvals or worker authority.

Current authenticated admin identity writers emit `admin_attested` and a fresh
verification timestamp. Public self-service requests cannot select that value.
The GitHub App bot writer uses it only after a fresh authenticated provider lookup
and exact bot/canonical-user checks; projection reads the committed database
method. Ordinary OAuth, verified magic-link and platform placement proof retain
their established behavior. The evidence inventory uses the same vocabulary and
continues to classify missing historical lifecycle evidence as unresolved.

No historical row is rewritten, deleted or automatically upgraded. Native/direct
account access and existing membership records remain intact; authority gained
solely through an ambiguous external link is withheld. Recover a legitimate link
through supported provider verification, or an authenticated administrator's
reviewed identity unlink/relink operation after checking the exact account and
tenant. Do not bulk relabel old rows or restore the old trust policy as rollback.

## Deployment order and evidence

Deploy the gateway and webhook trust vocabularies together. An old webhook will
refuse a new `admin_attested` projection, and an old gateway can still trust an
ambiguous label; a source merge alone does not retire the deployed vulnerability.
Review the corresponding images, in-flight work and identity projections during
controlled rollout. No database migration or automatic backfill is required.

The supervisor's consistent read-only snapshot covered 32 tenant scopes and 41
identity rows. It found one GitHub `admin_manual` row without matching legacy
lifecycle audit evidence; it was not relabeled or declared legitimate/illegitimate.
Snapshots and tenant identifiers are private. The initial classifier rejected an
empty optional team field; its bounded compatibility correction and offline
reclassification are separately tracked in PR #6075. This snapshot is point-in-time
evidence, not evidence that a deployment has occurred.

Regression coverage checks explicit legacy denial across workspace, budget,
approval, ARC and webhook consumers; stable routing and unchanged stored label;
new admin writer timestamps; and committed bot database/projection agreement.
