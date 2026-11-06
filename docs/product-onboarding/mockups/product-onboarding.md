# Product Profile & Onboarding prototype

Open [product-onboarding.html](product-onboarding.html) directly in a browser. It is
self-contained: no build, server or network connection is required. The preview
uses fictional local data and simulates saved state separately from unfinished
form input. It does not call ADP, cloud providers or the installed CLI.

This prototype accompanies [#8153](https://github.com/aws-e/adp/issues/8153) and
[#8141](https://github.com/aws-e/adp/issues/8141). Its
[experience contract](../design/experience.md), [API](../design/api.md),
[database](../design/database.md) and [shared data model](../design/data-model.md)
define implementation behavior. It is proposed for review, not a deployed feature.

For the current release (#8435), Product Context and L5 services both report
`unavailable` with reason `integration_deferred`. Saved Product/Profile work
continues; retries cannot queue peer actions or create success receipts. The
future Context/L5 handoff and result-readback design remains under #8266.

## Simple first save

Choose **Add product** and enter name, purpose and intended users. The owner and
first governed Project are filled in automatically. Domain and selecting an
existing Project are optional details. Incomplete input stays a browser draft;
Save creates a Product only when required information is present.

A minimal Idea is 100% of its two applicable checks, with maturity **Saved**.
Connections, team, environments and a financial plan can be added later. The
four sections are Product information, Connections, Environments and Financials.
Only selected/required checks count, and sources must be verified to complete them.
Getting Started requires a repository; Advanced adds an environment and pipeline;
Pro adds operations context and all selected checks. Completion is not authority.

## Portfolio, Product, Project, Initiative and Release

Product is the enduring offering. Project is a persistent delivery, operating and
evidence scope. The example Product includes Core platform and Developer CLI to
show that switching Project loads different connections, financials and sessions.
Product information and its team remain shared. A single-scope Product hides the
selector; **More options → Manage delivery scopes** adds or links another scope
and can remove an association while retaining the Project's assets. With multiple
scopes, the **Delivery scope** selector switches the canonical Project.

A Portfolio optionally collects Products, such as Commerce containing Checkout
and Accounts. A Product can belong to several Portfolios through dated membership;
Program remains a separate grouping of Initiatives. The relationship explanation
introduces Portfolio without adding a first-save field. Portfolio administration
and collection reports belong to the separate catalog/Context capability.

Initiatives normally span products. Each product's Contribution describes its
agreed outcome and work in its own Project. Releases select work/artifacts and can
link to several products; a release plan is separate from observed deployment.
None is a mandatory onboarding parent. The **How your work connects** explanation
uses the same model as Context and Engineering Insights; it does not pretend to
implement their planning, analytics or cross-Project execution services.

## Connections, financials and resume

- Source selection reuses a typed existing connection. Verification, partial
  discovery, unavailable/denied providers and revoked-source views are distinct.
  Expired evidence shows **Needs verification**, with matching stale progress,
  verified-source counts and export status; the original observation time remains.
  Unlinking retains the external asset/shared connection. Preview fixtures cover
  one resource per supported category; real providers supply pagination and IDs.
- Team creation shows its existing department. Team/person administration follows
  the preview role; an editor can request administrator help and continue other work.
  An existing verified organization member is distinct from enrolling a new identity.
- Repository creation previews organization, private visibility, template and
  protection policy before its separate simulated operation.
- Financials shows amount, currency and an editable three-month period. Product
  target, current owner, AI category and proposed funding are defaults. An existing
  cross-product Initiative and a release target date are optional details. The date
  does not create a release identity or substitute for the allocation period.
- Suggested financial fields are accepted individually. Existing values start
  unselected. Unconsumed proposals and each accepted field's source are retained;
  Add later and Not applicable have separate effects on progress.
- **Resume in CLI** shows the designed `adp onboard resume` / `adp product show`
  commands with Product, Project and session IDs. Its expandable edit simulation
  changes saved state independently, so a stale browser edit requires review.
- **Export profile** downloads a strict Profile JSON response for the selected
  Project, including typed configuration/provenance/progress and current filtering.
  This is not an Insights event, a Context snapshot or a deployment configuration.

## Preview scenarios

The toolbar's **Preview scenarios** controls are for reviewers, outside the product
workflow. Switch editor/admin, partial/unavailable/denied discovery, pause handoffs,
simulate offline input, lose an edit response, or revoke a selected repository.

Session section states reflect saved checks and pending operations. Untouched
optional sections remain `not_started`; Later and N/A keep their distinct meanings.
Pending handoffs and suggestions survive reload. Offline form drafts are retained
without claiming a saved Product change. Save conflicts show current and edited
values, preserve unrelated changes and require an explicit choice. A lost response
is reconciled using the original receipt instead of creating another mutation.
Untouched forms refresh after a shared Product edit; dirty drafts remain available
for conflict review.
The example uses a single browser, so these controls illustrate the state contract;
they do not establish real multi-user, authorization, OAuth or provider correctness.

Local data uses `adp.product-onboarding.prototype.v2`. The earlier v1 draft is left
untouched. Reset clears only v2 sample state. Browser storage policies can restrict
file persistence; the UI reports that case. No credential or real operator data is
needed or stored.

## Placement and verification

Keep the existing Blueprint shell, header, organization selector and navigation.
Add Products inside `MainLayout`; preserve Connections, Credentials, Organizations
and Budget & Spend. Production should use existing React/shared components. The
shell reference remains frontend revision `ac519dd2d4404d6dc87d9a0af28004c3debc187c`;
this preview does not assert the deployed UI revision or enabled feature set.

Run [verify_mockup.py](verify_mockup.py) with Python, `playwright` and `jsonschema`:

```bash
python -m playwright install chromium
python docs/product-onboarding/mockups/verify_mockup.py
```

For an existing Chromium binary pass `--browser /path/to/chromium`. Use `--output`
for screenshots/results; default is `/tmp/adp-onboarding-browser-check`. The checks
exercise creation limits, single-scope simplicity, scoped configuration, section
states, evidence expiry, clean-form refresh, verification/maturity,
partial imports and provenance, durable resume, conflicts, offline/lost-response
recovery, role-gated handoffs, revocation, Project management and responsive layouts.
Captured simulated requests/responses are checked against the actual design schemas,
including before reload. Also run the design validator for Context/Insights alignment.

These are executable prototype/contract checks, not deployed UI/CLI E2E acceptance.

The [consistency review](../design/consistency-review.md) maps each user behavior to
its API/schema, storage owner and lineage boundary.
