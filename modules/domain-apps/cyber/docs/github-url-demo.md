# Team demo: URL investigation through a GitHub issue

Use an ADP-connected repository and a human issue comment containing
`@agent-malware-analysis-agent`. This mention maps to the hosted cyber persona.
The separate `malware-analysis-agent` label workflow is designed around the
seven-stage file-analysis pipeline; use the hosted mention for this URL demo.

The browser investigation is deployed and has controlled live acceptance.
The newer analyst-context, enrichment and assessment-recovery changes are in
PR #5842 and require release before they can be demonstrated through production.
The current GitHub invocation, progress comments and artifact delivery must be
rehearsed together before calling this a verified live demo.

## Prepare the issue

Title: **Cyber agent demo: investigate these URLs**

Body (replace the second URL before posting):

```text
We are demonstrating evidence-led URL investigation to our team.

URLs:
1. https://example.com/ — a simple availability and reporting control.
2. <chosen reachable URL> — a site with relevant pages to investigate.

Research question: What does each site present, what information does it request,
who does it claim operates it, and what evidence supports or weakens concern?

Use one investigation case per URL. Follow relevant links on the supplied host
and its subdomains. Explain what each useful observation changed. Distinguish
observed behavior, page claims, external context and remaining uncertainty.

Post an acknowledgement, concise progress updates after significant discoveries,
and a final per-URL assessment on this issue. Publish the report and screenshots
through the normal run-artifact mechanism and include verified artifact links.
If a page is unavailable or challenged, report that result and its limitations.
```

Then add a human comment:

```text
@agent-malware-analysis-agent Please perform the URL investigations described
above using the url-analysis skill. This is URL analysis, not a file-sample task.
Keep us updated in this issue and finish with the findings and artifact links.
```

## Rehearsal and presentation

Choose two or three reachable pages before the meeting. `example.com` checks
the path but is too simple to show adaptive navigation. A clearly identified
controlled site with a form and operator-information page is a better second
case. An arbitrary feed of old phishing URLs often demonstrates unavailability
rather than investigation. Keep real datasets and captures in AWS/S3.

The rehearsal passes when the mention starts the intended hosted persona,
updates appear on the issue, the agent follows a relevant observed lead when
one exists, and the final report's links open for the presenter. Confirm the
findings against the screenshots and verify that the browser sessions ended.
Retain that dated issue/report as a fallback if a live site changes.

Show the issue and its updates, then a screenshot and the evidence-backed
finding it supports. Demonstrate how the agent's next action follows from what
it just observed. Describe conclusions as research findings for review, not a
validated detection-accuracy claim. A form's declared destination is not proof
that credentials were transmitted.

This document prepares the demo; creating the issue and triggering its comments
are separate actions. No rehearsal issue was posted by writing this guide.
