# Agent-directed domain investigations

The researcher supplies a seed URL and a question. The existing cyber agent
queries Common Crawl through Athena, records an initial hypothesis, then
inspects live evidence, chooses a useful next browser action, and
revises its assessment from the result. It can explore multiple pages in one
browser context. In native mode, the worker connects directly to AgentCore Browser.
The model selects actions; the local driver records evidence and manages cleanup.

For example: “Investigate this account-verification link. Determine what it asks
for, who operates it, and whether the claimed brand affiliation is supported.
Follow relevant pages within this domain and cite the evidence.”

## Investigation loop

| Stage | Agent decision | Evidence retained |
| --- | --- | --- |
| Archive context | Examine historical index coverage and metadata | Query ID, crawl partitions, sampled records and limitations |
| Selected archived pages | Choose indexed captures whose content answers a question | S3 WARC range, original payload, extracted text/forms/scripts, dates and hashes |
| Initial hypothesis | Identify a question to test against the current site | Source-linked hypothesis before any browser lease starts |
| Seed | Identify unanswered questions from the landing page | Initial screenshot, DOM, forms, scripts, links, requests |
| Review | Support, refute or revise a hypothesis | Concise explanation and actual observation IDs |
| Next action | Select a relevant observed link/control, root, back, scroll or wait | Research question, reason and expected signal before execution |
| Reassessment | Compare the result with the hypothesis and counterevidence | Further observations and hypothesis history |
| Handoff | Explain why investigation stopped and close the context | Findings, path, coverage, unresolved leads and verified artifacts |

The agent must review new evidence before selecting the next action. It does not
receive a fixed URL list or automatically crawl every link. Browser choices have
IDs tied to the current view; stale or invented choices are refused. The session process
keeps exact destinations private, so query redaction does not break link navigation.
Cookies, session storage and history survive between decisions. A profile change
creates a new context and retains both sets of evidence in the same case.

The default `observed_external` scope allows the model to follow relevant observed
links and redirects across public hosts, including sibling hosts. The researcher
can explicitly select `host` to restrict navigation to the seed hostname and its
subdomains for analyst-selected actions. Native mode does not filter all page
requests or redirects. Private-service probing remains outside the task. Forms,
credentials, downloads and challenge bypass remain excluded from this transport.
Supported controls are observed disclosure elements and tabs, not arbitrary
selectors or JavaScript supplied by the agent.

## Runtime interface

The full workflow and JSON review/decision shapes are in the
[agent skill](../agent/skills/url-analysis/SKILL.md).

```bash
python /app/skills/url-analysis/domain_investigation.py prepare "$SEED_URL" \
  --case "$CASE_DIR" --objective "$RESEARCH_QUESTION"
python /app/skills/url-analysis/domain_investigation.py hypothesize \
  --case "$CASE_DIR" --hypothesis "$HYPOTHESIS_FILE"
python /app/skills/url-analysis/domain_investigation.py browse --case "$CASE_DIR"
python /app/skills/url-analysis/domain_investigation.py review \
  --case "$CASE_DIR" --review "$REVIEW_FILE"
python /app/skills/url-analysis/domain_investigation.py step follow \
  --case "$CASE_DIR" --candidate-id "$CHOICE_ID" --decision "$DECISION_FILE"
```

Inspect each result before writing the next review and decision. Additional
operations are `step expand`, `root`, `back`, `scroll`, `wait --seconds N`,
`profile mobile`, `status`, `close`, `assess`, and `verify`. There are at most
12 steps per 600-second context and two contexts/24 steps per case. Agent reasoning
time consumes the lease. The local Playwright process pumps browser events while the agent reasons
and retains network/navigation evidence. It closes abandoned contexts on expiry.
A lost context is reported; it is not recreated and its actions are not replayed.

The worker stores the session capability privately under `/tmp/adp-url-browser-leases`,
outside run artifacts. The capability is removed after confirmed close. The report
contains session IDs and cleanup outcomes, never the capability or CDP endpoints.

Native sessions use private Unix sockets within the worker pod between CLI calls.
There is no HTTP broker service or alternate browser identity. Each session runs
in a supervised process. The supervisor can kill a stalled driver and stop its
AgentCore session independently. The managed session timeout is the backstop if
the entire pod disappears. `capture` and `analyze` remain supported.
Explicit broker mode supports installations awaiting migration.

## Researcher output

The same complete artifact bundle now includes the investigation path, decisions,
hypothesis updates, external leads and explicit stopping reason. Case JSON records
relationships between observations/pages. The HTML/Markdown reports show findings
and the investigation narrative alongside screenshots and evidence. File hashes,
indicator CSV and provenance are retained. Long target URLs wrap in the HTML report.

The model owns the overall verdict from Common Crawl, browser observations and
other sourced context. Report structure, reference existence and artifact hashes
are checked; finding semantics, evidence sufficiency and verdicts are not
adjudicated by application code. Partial captures, HTTP errors and unconfirmed
cleanup remain visible without forcing an inconclusive result. Cleanup is always
attempted and its outcome is reported separately.

Use `no_specific_concern`, `suspicious`, `malicious` or `inconclusive` for the overall
assessment. The existing `no_adverse_behavior_observed` label remains available for
browser-limited conclusions. An archive-only assessment must state that live page
behavior was not verified. Index rows and selectively retrieved archived page
content have separate source IDs.
A declared form action is configuration, not observed transmission. The model
must consider counterevidence and state uncertainty; reference checks do not prove
that a claim is correct.

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

Native release requires a compatible worker image and regional Browser lifecycle/
CDP permissions. Validate native collection, then drain legacy investigations and
scale the broker to zero. Preserve running jobs and the separate protected-worker
migration hold. Follow the canonical deployment guide and use reviewed scoped
saved plans for the relevant resources.
See [Common Crawl setup and runtime recovery](common-crawl-investigation.md) for
the archive configuration, query bounds and isolated-process acceptance requirements.
