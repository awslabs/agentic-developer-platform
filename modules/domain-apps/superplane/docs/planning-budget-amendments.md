# Reviewed initial-planning daily budget amendments

An original planning request can have a daily allocation limit too low for the
infrastructure it will propose. Its admitted request, preview revision, consumed
approval, reservations and execution history remain immutable.

The original requester reviews an explicit higher limit using
`POST /workspaces/{workspace_id}/planning-budget/review`, supplying the current
operation version, a fresh idempotency key and `daily_usd`. The selected original
human approver decides that separate ticket using `/planning-budget/decision`.
The review shows both limits and the original operation/digest. Current human
grants, expiry, distinct-approver policy and the exact workspace budget observed
at review are checked again. The decision atomically updates the workspace's daily
budget projection. Immutable reauthorization history records the terms and
approver; one approved amendment is permitted per original operation. Replaying a
decision cannot raise the limit again. There is no budget-resume endpoint.

This path is limited to an expired initial planning attempt, with an unresolved
initial intent and no cluster registered. It does not reset or recover execution,
raise its attempt ceiling, renew its old grants, or approve infrastructure apply.
Normal protected recovery remains a separate decision.

The worker reads the current amendment against original approval consumption,
request digest and workspace scope before planning and again before publishing
its artifact. Revocation, expiry, a lowered workspace budget or changed authority
refuses use. The artifact retains the original admission and explicitly names the
amendment. Its continuation proposes the new allocation cap and preview revision;
other lifecycle inputs stay fixed. API and worker validate that linkage. Actual
apply still requires a new human approval of the exact saved artifact and the new
spend envelope through ordinary shared admission.

Migration `049_planning_budget` adds nullable amendment terms to reauthorization
history and a separate scope with a unique approved-amendment constraint. The
existing immutable-history trigger protects the terms and decision. Upgrade API
and worker together through the installer. Downgrade refuses after any budget
amendment history has been recorded.

### Confirming the same cap during a later recovery

A later recovery may occur after the original amendment expires. The original
amendment remains immutable, including its expiry. A fresh `unstarted-planning`
review explicitly displays `planning_budget_confirmation`: the original budget
amendment ID and unchanged daily limit. Its selected human decision requires
current `workspace:administer` authority as well as ordinary recovery authority.
It cannot change the amount or workspace projection. Withdrawal of the original
budget decision or approval, changed operation/version or a lowered workspace
budget refuses confirmation.

The worker accepts this confirmation only through the latest immutable Harness
provider-retry receipt, bound to the same operation, digest, original approval,
requester, approver and reviewed version. A pending or even approved but unconsumed
recovery ticket gives no budget authority. The confirmation's own expiry and
revocation remain enforced. Saved artifacts retain its ID and decision timestamp;
subsequent artifact checks must find the same current authority. Exact-plan apply
approval remains separate.

Migration `050_planning_budget_confirmation` permits immutable terms on planning
recovery rows while preserving the one-approved-budget-amendment constraint. It
refuses downgrade after a confirmation has been recorded. Install API, worker and
schema together before reviewing or consuming confirmations.
