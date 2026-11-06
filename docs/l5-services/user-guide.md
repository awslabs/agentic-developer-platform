# Security in the Product workspace

The **Context & services → Security** page belongs to the selected Product and
Project. Open the Product workspace, select the Project, then choose **Context &
services**. Existing Product details and authorized source revisions are reused;
you do not need to repeat onboarding. Security availability depends on compatible
L5 APIs, delegated execution and provider access. A saved Product or a visible
Security page does not establish that a provider is ready or a scan has run.

## Configure without starting a scan

1. In **Security configuration**, review **Inherited from Product**, **Source
   revisions**, **Missing requirements** and **Unavailable choices**. Select a
   capability, an authorized AWS connection and a region. Choose source
   revisions explicitly when you need to override the inherited selection;
   explain changed connection, region or source choices in **Reason for explicit
   Security choices**. If a needed option is unavailable, resolve the listed gap
   rather than substituting another connection or claiming the setup is ready.
2. Set visibility and **Enable configuration** as appropriate. Optionally set
   the assessment monitoring policy: poll interval, grace and suspected-stall
   threshold. These settings report progress; a suspected stall never restarts
   an assessment automatically. Choose **Save Security configuration**, then
   use **Refresh Security configuration** to check its stored revision and
   effective source choices. Saving does **not** provision a provider binding,
   start a scan or publish a result.
3. After an eligible binding is ready, return to **Security configuration** to
   allow manual runs. Select **Allow manual assessments**, a **Trigger
   capability**, a ready **Trigger binding** and compatible **Trigger source
   revisions** from the saved source selection, then save again. Missing choices
   keep the trigger or run unavailable.

## Manage a provider binding

In **Provider binding**, check the selected connection and region, then choose
**Create provider binding** or provide an existing resource ID and revision and
choose **Link existing resource**. These are separate, explicit operations.
Inspect **Existing bindings** for ownership, current revision, readiness and
gaps. Select a binding to check, update, suspend, resume or unlink it. **Unlink**
retains the resource; **Delete owned resource (confirm)** is available only for
an ADP-created binding and requires a separate deletion confirmation. Never
assume that an accepted operation means provider setup or deletion succeeded:
refresh the binding and operation state. If an operation outcome is unknown,
reconcile the original operation with a reason rather than starting another
potentially paid create. An unconfirmed request offers **Retry original receipt**;
do not retry it as a new request.

## Start and inspect an assessment

In **Security assessments**, choose **Assessment capability**, **Assessment
binding** and the **Pinned assessment sources**. A matching enabled manual
trigger, ready binding and selected source revisions are needed before **Start
Security assessment** is available. Submitting is the explicit step that asks
for a run; admission is not completion. Inspect **Visible assessments** to
separate local run state, provider state, native job status, phases and task
counts. The last attempted and successful checks, next check, monitoring policy
and suspected-stall indicator describe freshness, not a successful result.
An active or unknown run must be inspected before asking for another paid run.
Cancellation, reconciliation and checked retry require an explicit reason.

Follow **Inspect visible results for this assessment** to **Security results**.
Select a visible result to inspect its run, source snapshots, provider outcome,
coverage, reported finding count and publication state. An empty list, zero
findings, failed source read or partial coverage is **not** evidence of a passed
scan. Select findings to inspect native evidence and candidate severity
separately from human validation and triage. A triage edit requires a reason
and the current revision. Comparing with a baseline reports comparability;
partial or incomparable coverage does not establish resolved findings. A
publication marked unavailable or waiting for retry is not confirmed. Retry
**publication only** with a reason; if the reply is uncertain, use **Retry
original publication receipt**, not a new assessment.

## Recover from changes or lost access

A **changed elsewhere** / 412 message means another client saved a newer
configuration, binding or triage revision. Refresh that record, inspect the
latest values, and make a **new deliberate edit** before saving. The page does
not replay stale fields or silently overwrite a CLI change. Configuration
conflicts temporarily withhold downstream run choices until a fresh read.

If Security access is denied or a previously visible record becomes
inaccessible, the workspace removes Security fields, runs, findings and counts.
**Refresh Security access** performs a new read; it does not restore cached
results. An inaccessible or foreign Product is not proof of an empty or clean
scan. Ask a Product administrator to restore the appropriate authorization if
refresh still fails. If admission reports unavailable, do not treat the request
as completed; retain the original receipt and inspect existing runs before
retrying. Actual provider operation and live acceptance require the separately
authorized deployment and current private target.
