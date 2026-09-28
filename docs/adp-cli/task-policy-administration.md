# Task policy administration

Organization administrators can open **Organizations → Structure → Service accounts → Task policy** to inspect and edit a registered service account's Task policy. The editor resolves the roster identity to its canonical principal in the selected organization. Missing or conflicting registrations must be resolved before enrollment; the editor never guesses which account to modify.

The policy controls Task access, allowed personas, tools, Task API permissions, maximum spend per Task, runtime, model turns and output tokens per turn. Saves use the current policy version and write an audit record atomically. A concurrent edit requires reloading. An uncertain save also requires reloading before retrying.

Model preferences are separate. **Agent Models** displays the Task budget for the selected personal or service identity and previews the draft model's conservative per-request reservation. Personal selections do not configure a service account. The UI reports unavailable previews explicitly; model availability alone does not establish budget compatibility.

## Platform ceiling

The deployment operator sets `task_max_usd_per_task` in the webhook infrastructure configuration. It is published in the gateway configuration as `ADP_TASK_MAX_USD_PER_TASK`. The platform ceiling defaults to USD 1,000. This is the maximum an administrator can configure, not a default grant: existing identity Task limits and organization budgets are unchanged. Values must be finite and positive; invalid configuration refuses policy writes. The UI displays the configured ceiling. The Task policy store is the authoritative validator for all policy-writing APIs, including human enrollment.

This is an **enrollment/edit ceiling**, not an organization budget. Changing it does not rewrite existing policies, increase any account's spending authority, or terminate existing Tasks. An organization administrator must explicitly save a new service-account budget within the ceiling. Organization, department, team and identity budget enforcement still applies. New Tasks snapshot their admitted limits; current authorization checks continue to apply to running Tasks.

For example, a USD 12.122880 per-request reservation cannot fit in a USD 1 Task budget. An operator may configure an appropriate ceiling, after which an authorized administrator can set an account's Task budget. Raising an account's budget is a spending-policy decision, not a requirement to enable the UI. No budgets or model selections are automatically raised by this release.

## Reservation calculation

The preview invokes the same local Anthropic quote adapter used at dispatch, without invoking a model or reserving funds. It prices the full published context capacity at the highest applicable published input/cache-write rate and the policy's maximum output tokens. The request at dispatch is quoted again against its exact bytes and current pricing revision. Request-specific unsupported features may still refuse dispatch.

The full-context bound is deliberately retained: approximate token counts do not bound hidden provider framing, media, or cache-write charges. Replacing it requires a trustworthy provider token-count contract bound to the actual forwarded request, including all billable input and features. A preview is not an actual charge, a promise of successful admission, or an eight-turn total: the Task's cumulative spend and outstanding reservations remain bounded separately.

Saving a Task policy does not add OAuth scopes to a Cognito client, register aliases, grant hierarchy assignments or change model preferences. Those identity requirements remain independently enforced.

## Model selection authorization

Task enrollment authorizes explicit model-selection revisions. Changing a selection can make that authorization stale even when the budget is sufficient. The editor displays the current model and warns about a stale revision. The administrator can explicitly check **Authorize the displayed model selections for enabled personas when saving** to renew authorization. A concurrent model change still fails closed at admission and requires reloading and renewed authorization.

New policies may carry `model_policy_versions`, a persona-to-revision map, allowing different Task personas to have independent authorized selections. Existing policies retain their legacy `model_policy_version` fallback. This change does not weaken the binding of running Tasks to their admitted model revision.

Platform execution ceilings are 1,000 model turns, 360 minutes (6 hours), and 10,000 output tokens per model turn. Existing policies retain their configured limits; persona execution limits and model capabilities can impose lower bounds.

Task submission no longer reserves the policy's full allowance against pilot qualification or tenant-day budgets. The former $25 qualification and $10/day Task caps are retired. Each model request still reserves its conservative cost against the admitted Task limit and the enforced organization, department, team and identity budgets before provider dispatch. Existing pilot reservations are retained for settlement and cleanup; changing deployments does not erase their accounting.
