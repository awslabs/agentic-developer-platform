# URL analysis: May agent runs compared with September implementation

Reviewed 2026-09-24 in `aws-e/adp`. This is a source comparison, not a new live
benchmark. It contains summaries and source links, not target URL datasets or
copied captures.

The May agent produced broader analyst reports from browser evidence, submission
context and enrichment. September changed both the browser interface and the
assessment policy. The latest 100-case evaluation exercises a narrower tool
adapter than the full hosted agent used for the May issues. These differences
explain lost analytical capability, but the old reports do not establish a higher
measured accuracy or consistently deeper adaptive browsing.

## Historical issues and what they demonstrate

All dates below are 2026. Reports are agent-authored claims unless stated otherwise;
their original screenshots, browser logs and external intelligence have not been
independently revalidated in this review.

| Issue | Date | Evidence relevant to the comparison |
|---|---|---|
| [#494](https://github.com/aws-e/adp/issues/494) | May 5 | Three-case post-fix smoke test; explicitly supplied expected outcomes. Useful operational history, not blind accuracy evidence. |
| [#497](https://github.com/aws-e/adp/issues/497) | May 5 | Blind three-case smoke and later CDP rerun. Demonstrates working Playwright access after IAM/SDK fixes. |
| [#500](https://github.com/aws-e/adp/issues/500) | May 6 | Four URL reports: BitMart impersonation, phishing warning, unreachable suspected malware endpoint, legitimate government page. Reports three malicious and one clean. |
| [#503](https://github.com/aws-e/adp/issues/503) | May 6 | Three reports, including an Apple-themed password form. Explicit comparison between deterministic scores and analyst overrides. |
| [#505](https://github.com/aws-e/adp/issues/505) | May 6 | Four reports, including a spinner-only page classified malicious and an inaccessible storage object classified suspicious from submission context. Shows both contextual reasoning and overstatement. |
| [#511](https://github.com/aws-e/adp/issues/511) | May 6 | Three-case evidence-bucket validation, including reports from incomplete browsing. |
| [#513](https://github.com/aws-e/adp/issues/513) | May 6 | Three-case run plus explicit S3 object verification after identity-side permissions were repaired. |
| [#515](https://github.com/aws-e/adp/issues/515) | May 6 | Three-case end-to-end validation. Summary records analyst overrides, six S3 objects and three terminated sessions. |

The complete comment lists for #500, #503, #505 and #515 were retrieved; their
counts matched GitHub's REST issue metadata (24, 15, 12 and 11 respectively).

### Concrete historical examples

- **Visual/contextual interpretation:** [#500, URL 1](https://github.com/aws-e/adp/issues/500#issuecomment-4385948604)
  reports a BitMart login imitation on unrelated hosting even though standard
  form extraction returned nothing. It combined the screenshot/title with the
  submitted SMS context. The report's further claim that empty text proved
  deliberate anti-scraping was not established by that observation.
- **Useful form interpretation:** [#503, URL 2](https://github.com/aws-e/adp/issues/503#issuecomment-4386318797)
  describes an Apple-themed username/password form and its declared POST action,
  together with a reported Apple-support lure. It inferred phishing rather than
  simply returning the scorer's output. However, markup did not prove that a
  credential submission occurred or that the entire hosting domain was compromised.
- **Explicit analyst override:** [#503 summary](https://github.com/aws-e/adp/issues/503#issuecomment-4386324621)
  records `suspicious/40` becoming `malicious/88` for that form. The agent, rather
  than the deterministic scoring helper, supplied the final interpretation.
- **Warning-page evidence:** [#515, URL 2](https://github.com/aws-e/adp/issues/515#issuecomment-4388761182)
  records `clean/30` becoming `malicious/92`, based on an explicit suspected-phishing
  warning, its DOM and supplier-impersonation submission context. It did not bypass
  the warning. This supports retaining warning evidence; it does not independently
  prove the warning provider's attribution or the hidden page's behavior.
- **Overconfident incomplete-page verdict:** [#505, URL 2](https://github.com/aws-e/adp/issues/505#issuecomment-4386424656)
  reports malicious credential harvesting from an account-page title, loading
  spinner, hosting details and an email report. It admits no form was observed and
  suggests a longer wait might reveal one. This is not evidence of successful
  adaptive follow-up, and the claimed theft/evasion exceeds the captured facts.
- **Context despite unavailability:** [#500, URL 3](https://github.com/aws-e/adp/issues/500#issuecomment-4385955673)
  reports malicious from an unreachable endpoint plus submitted EDR context.
  [#505, URL 3](https://github.com/aws-e/adp/issues/505#issuecomment-4386428084)
  reports suspicious for a 403 response and an alleged salary-increase lure.
  These retain useful incident context, but a timeout/403 alone does not establish
  malicious infrastructure, takedown or prior page content.

## What changed in code

Historical reference: [May skill at `0e2cb087b`](https://github.com/aws-e/adp/blob/0e2cb087be58c3a926e708de18d65d73a3928ef3/modules/domain-apps/cyber/agent/skills/url-analysis/SKILL.md)
and [May persona](https://github.com/aws-e/adp/blob/0e2cb087be58c3a926e708de18d65d73a3928ef3/modules/domain-apps/cyber/agent/personas/malware-analysis-agent.md).
This is the post-#504 version used by the later May runs; #500 predates that
specific screenshot-guidance update but its comments explicitly report CDP use.

Current main was checked at `c4635f45a26598082b20bdb0c492067b8b14c827`.
Latest evaluation branch: `74169126c07bb45a18b72442b244d2b37828132a`,
[PR #5842](https://github.com/aws-e/adp/pull/5842), still draft/open and unmerged at
review time. The URL persona section, partial-evidence validator and evidence-item
rules discussed below are shared with main; the live evaluation adapter is on
the draft branch. This review did not audit the currently deployed worker image.

| Area | May agent | Current URL workflow / latest benchmark |
|---|---|---|
| Agent execution | Full hosted agent, URL analyst persona, issue context; skill allowed Bash, Read, Write and WebFetch. | Hosted agent still has a URL persona and CLI. The live benchmark uses the skill plus an evaluation prompt, not the full hosted runtime/persona. |
| Browser control | Agent wrote Python orchestration at runtime, with direct AgentCore/Playwright CDP access and an OS-action fallback. | Broker owns CDP. Model selects observed choices through follow, expand, root, back, scroll, wait and profile operations. No arbitrary scripts/selectors or direct fallback. |
| Adaptive investigation | Broad scripting freedom. Reviewed reports mostly document one session/capture per URL, not a demonstrated multi-page hypothesis loop. | Explicit persistent session, evidence reviews and model-selected next actions. The recent run demonstrates actual model navigation, but limited depth. |
| Enrichment | WHOIS/RDAP, DNS, CT, VT, URLhaus and MISP were standard steps, with graceful degradation. | Existing enrichment code remains; URL workflow treats it as optional. CLI supports verified brand records and VT lookup. Live benchmark exposes neither and removes corroboration from model input. |
| Submission context | Detailed alleged SMS/email/EDR/NDR context and sibling campaign reports supplied in issues. | The public-feed benchmark does not reproduce those full incident narratives. Their existence in old issues is not independent verification of the telemetry. |
| Decision policy | `clean/suspicious/malicious`, heuristic confidence and analyst overrides; ambiguous evidence generally leaned suspicious. | `no_adverse_behavior_observed/suspicious/malicious/inconclusive`; evidence-linked findings, strict incomplete-view handling and no invented confidence. |
| Warnings and failed captures | Warning screens and incident context could support a strong conclusion despite partial browsing. | CAPTCHA, phishing warnings and deceptive-site warnings share one challenge flag; flagged partial evidence cannot support an adverse assessment. Zero observations skip the model. |
| Reports and evidence | Detailed incident handoff, IOCs, ATT&CK and actions; some early runs lacked durable S3 uploads. | Structured case, provenance, hash-checked evidence, navigation/review history and coverage. Latest run has verified S3 persistence, but assessment fallback can lose useful findings. |

The history identifies distinct changes, rather than one undifferentiated broker
regression:

1. [#490 / PR #491](https://github.com/aws-e/adp/issues/490), May 5, added the URL
   analyst heuristics and handoff guidance.
2. [#495 / PR #496](https://github.com/aws-e/adp/issues/495), May 5, deliberately
   removed fixed orchestration wrappers and let the agent write orchestration
   from a browser API contract. It left the deterministic scorer unchanged.
3. [PR #5721](https://github.com/aws-e/adp/pull/5721), September 22, introduced
   connection-boundary enforcement and a broker that initially completed capture
   and closed the browser before responding. This addressed concrete private-address,
   redirect and DNS validation bypasses; direct CDP freedom was reduced here.
4. [PR #5782](https://github.com/aws-e/adp/pull/5782), September 23, replaced the
   old URL persona section, including mandatory enrichment and its analyst
   heuristics, with evidence-case assessment rules. Enrichment became optional.
5. [PR #5808](https://github.com/aws-e/adp/pull/5808), September 23, introduced the
   maintained persistent, agent-directed investigation commands. Therefore the
   current browser is not always closed before the model can choose another step.
6. [PR #5823](https://github.com/aws-e/adp/pull/5823) added intact evidence-item
   references so some partial captures can support findings. [PR #5842](https://github.com/aws-e/adp/pull/5842)
   adds the live model-driven evaluation and further reasoning guidance; it remains unmerged.

## Specific current limitations

- [Live adapter](../agent/skills/url-analysis/live_evaluation.py): `tool_contracts()`
  exposes only `advance`, `inspect_evidence`, `profile` and `finish`.
  `investigate()` reads `SKILL.md` for its system prompt; `evidence_view()` removes
  `assessment` and `corroboration`. Adding the old persona alone would not restore
  tools or incident context, and the current persona itself no longer contains
  the May URL heuristics.
- [Capture](../agent/skills/url-analysis/case_capture.py) groups `suspected phishing`
  and `deceptive site ahead` with `captcha` and `verify you are human` under
  `challenge_or_interstitial`. [Evidence validation](../agent/skills/url-analysis/evidence_items.py)
  excludes that flag from usable partial captures.
  [Assessment validation](../agent/skills/url-analysis/case_contract.py) rejects a
  non-inconclusive verdict when any cited finding uses such a view. This also
  affects benign-context findings included alongside earlier adverse evidence.
- [Investigation browser](../agent/skills/url-analysis/investigation_browser.py)
  prevents another step after a challenge. The broker also blocks service workers
  and WebSockets and proxies vetted HTTP connections. Those transport differences
  can affect page fidelity; their contribution to the current benchmark has not
  been isolated by a controlled comparison.

In the latest 100-case run, 65 cases produced no observations and skipped the
model. There were 35 model-invoked cases, 15 browser actions across 13 cases, and
10 successful-page cases versus 5 in the earlier snapshot run. Both runs ended
at 3 malicious / 97 inconclusive. These are outcomes and availability counts,
not a measured 3% classification accuracy.

The previous run audit identified one concrete evidence-handoff failure:
`case-125` had usable earlier evidence, then a model-selected wait produced a
challenge view. The model cited both observations in malicious and suspicious
assessment attempts; validation rejected both, and the case ended inconclusive.
Earlier usable evidence should remain reportable with precise citations even
when a later view is unavailable. The current rule does not globally invalidate
all earlier evidence; the observed failure involved citations and error recovery.

Benchmark audit: `s3://adp-dev-url-analysis-evidence-v2-879318057152/tenant=adp-default/issue=0/run=cyber-live100-v2-20260924/report.md`.
Datasets and captures remain in AWS/S3.

## Recommended recovery, in priority order

1. Evaluate the hosted cyber agent with its actual URL reasoning instructions,
   incident context and supported tools. Keep a separately named browser-only
   benchmark when isolating browsing performance. Compare like-for-like inputs.
2. Preserve useful analyst behavior: choose an investigation lead, inspect the
   result, update the hypothesis, and explain the next uncertainty. Restore
   sourced enrichment as a model-selectable tool when it answers that uncertainty.
   Keep observed page behavior, externally reported reputation and incident-context
   risk distinct, so a failed page does not erase all useful analysis.
3. Represent explicit threat warnings separately from human-verification
   challenges. Record what the warning says and its provenance without bypassing
   it or equating it with observed credential theft.
4. Make assessment correction preserve earlier valid findings. Validation should
   identify the problematic reference, and the agent should correct that reference
   while retaining the later coverage limitation.
5. Use controlled multi-page cases to test navigation, delayed rendering,
   alternative leads, counterevidence, warning handling and evidence preservation.
   Then compare fresh, available public cases with benign controls. Maintain any
   real targets and captures in S3. Inspect useful decisions and conclusions,
   not just action counts or the number of malicious labels.

Do not restore weak rules that equate a spinner, CDN, country, high port or timeout
with malicious intent, or a familiar domain with safety. Keep the broker's
connection protections while improving the investigation interface and reasoning.

The current [architecture document](architecture.md) mixes dates: it labels itself
as built in May, but its May #500 example now describes broker-owned CDP. Its
"4/4 correct" statement is not an independently scored accuracy result. Historical
commit contents and issue comments are the sources for this comparison. The exact
model/runtime identity of each May run was not verified, so no difference here is
attributed to a model downgrade.
