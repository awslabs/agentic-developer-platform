# Agent-directed domain investigations

The researcher supplies a seed URL and a question. The existing cyber agent
inspects evidence, forms a hypothesis, chooses a useful next browser action, and
revises its assessment from the result. It can explore multiple pages in one
browser context. The broker executes individual actions and enforces boundaries;
it does not choose the route or instantiate another model.

For example: “Investigate this account-verification link. Determine what it asks
for, who operates it, and whether the claimed brand affiliation is supported.
Follow relevant pages within this domain and cite the evidence.”

## Investigation loop

| Stage | Agent decision | Evidence retained |
| --- | --- | --- |
| Seed | Identify unanswered questions from the landing page | Initial screenshot, DOM, forms, scripts, links, requests |
| Review | Support, refute or revise a hypothesis | Concise explanation and actual observation IDs |
| Next action | Select a relevant observed link/control, root, back, scroll or wait | Research question, reason and expected signal before execution |
| Reassessment | Compare the result with the hypothesis and counterevidence | Further observations and hypothesis history |
| Handoff | Explain why investigation stopped and close the context | Findings, path, coverage, unresolved leads and verified artifacts |

The agent must review new evidence before selecting the next action. It does not
receive a fixed URL list or automatically crawl every link. Browser choices have
IDs tied to the current view; stale or invented choices are refused. The broker
keeps exact destinations private, so query redaction does not break link navigation.
Cookies, session storage and history survive between decisions. A profile change
creates a new context and retains both sets of evidence in the same case.

The default navigation scope is the supplied hostname and its subdomains. Other
public hosts may supply subresources through the guarded transport. Related
external navigation requires the researcher to request `observed_external`
scope; otherwise those links remain leads. Private destinations remain prohibited
in either mode. Forms, credentials, downloads and challenge bypass are excluded.
Supported controls are observed disclosure elements and tabs, not arbitrary
selectors or JavaScript supplied by the agent.

## Runtime interface

The full workflow and JSON review/decision shapes are in the
[agent skill](../agent/skills/url-analysis/SKILL.md).

```bash
python /app/skills/url-analysis/domain_investigation.py start "$SEED_URL" \
  --case "$CASE_DIR" --objective "$RESEARCH_QUESTION"
python /app/skills/url-analysis/domain_investigation.py review \
  --case "$CASE_DIR" --review "$REVIEW_FILE"
python /app/skills/url-analysis/domain_investigation.py step follow \
  --case "$CASE_DIR" --candidate-id "$CHOICE_ID" --decision "$DECISION_FILE"
```

Inspect each result before writing the next review and decision. Additional
operations are `step expand`, `root`, `back`, `scroll`, `wait --seconds N`,
`profile mobile`, `status`, `close`, `assess`, and `verify`. There are at most
12 steps per 300-second context and two contexts/24 steps per case. Agent reasoning
time consumes the lease. The broker pumps browser events while the agent reasons
and retains network/navigation evidence. It closes abandoned contexts on expiry.
A lost context is reported; it is not recreated and its actions are not replayed.

The worker stores the broker capability privately under `/tmp/adp-url-browser-leases`,
outside run artifacts. The capability is removed after confirmed close. The report
contains session IDs and cleanup outcomes, never the capability or CDP endpoints.

The broker exposes `/v1/investigation/start`, `/step` and `/close` over its existing
internal service. Each lease runs on one owning thread because Playwright's sync
API is thread-affine. Service `ClientIP` affinity routes a worker's steps to the
owning replica. The broker Pod opts out of voluntary Karpenter consolidation to
avoid disrupting active contexts. Unexpected replica loss still fails closed;
the managed 300-second session timeout is the cleanup backstop. Legacy `/v1/capture`
and `/v1/analyze` operations remain compatible.

## Researcher output

The same complete artifact bundle now includes the investigation path, decisions,
hypothesis updates, external leads and explicit stopping reason. Case JSON records
relationships between observations/pages. The HTML/Markdown reports show findings
and the investigation narrative alongside screenshots and evidence. File hashes,
indicator CSV and provenance are retained. Long target URLs wrap in the HTML report.

A declared form action is configuration, not observed transmission. Manual link
navigation is identified separately from site redirects. Adverse findings on a
partial page require intact, hash-checked evidence item references, explicit
coverage limitations and confirmed cleanup; no-adverse requires
all steps/observations complete and describes only the tested views. Counterevidence
and legitimate identity-provider relationships must be considered. Semantic accuracy
still requires researcher review; reference validation cannot prove every sentence.

The CLI persists locally. The agent must use the existing run-artifact publisher
and verify success before claiming durable delivery. No new UI or automatic case
upload is introduced by these tools. Normal UI/agent delivery needs its own
end-to-end acceptance.

## Verification and release

Real Chromium fixtures exercise the same recorder, HTTP broker and guards, with
only the managed session adapter and target HTTP transport replaced. They verify
session storage across clicked links, multiple pages, exact private link parameters,
operator disclosures, profile branching, evidence reviews, stale choices, scope,
private-address refusal, step caps, and uncertain-start cleanup reporting.

Two manual Bedrock model evaluations also exercised the decision loop:

- An undisclosed-operator fixture led the model from the seed to a verification
  form and then operator disclosure, with a revised hypothesis and suspicious
  assessment limited to the synthetic evidence.
- A disclosed-provider fixture used different links and page paths. The model
  inspected operator information, returned to the seed, examined the form, and
  revisited the disclosure. It considered the authorized-provider counterevidence
  and reported no adverse behavior observed within the tested fixture.

These are small acceptance examples of action selection and evidence use, not a
measurement of threat-detection accuracy. They used a real Bedrock model and local
guarded Chromium, not the deployed UI/GitHub entrypoint. No public Lambda fixture
is permitted or needed. Fixture CI stays on `arc-runner-org` without AWS credentials.

Release requires the updated broker image and Service affinity before workers
receive the new skill. Build one immutable runtime, roll out the backward-compatible
broker first, then pin new workers to the same digest. Preserve running jobs and the
separate protected-worker migration hold. Follow the canonical deployment guide
and use only reviewed scoped saved plans for the relevant resources.
