# Evidence review after each BOLT

Agents execute the required UI/installed-CLI journeys and independent API checks,
then publish one BOLT packet through the existing verification stories. The human
reviews the results and accepts or rejects the evidence. Running commands or
repeating a browser journey is optional, not an acceptance obligation.

The packet contains:

1. A short action → expected result → actual result report and pass/fail summary.
2. UI screenshots/video and trace where available; exact installed CLI commands,
   stdout/stderr and exit codes; independent API/result readback, as required by
   each story. Keep UI and CLI results separate.
3. AC → scenario/test → run/attempt → evidence links and the requirements/source,
   workflow, deployment, schema and applicable UI asset/CLI checksum identities.
4. All required positive, negative, retry/revocation/recovery results and cleanup
   receipts. Missing, skipped or failing required results stay incomplete.
5. Authorized artifact links and optional rerun steps. If a reviewer chooses to
   rerun, agents supply a fresh isolated fixture, setup and cleanup. The ordinary
   review uses recorded evidence and needs no active demo fixture.

Private evidence stays in authorized artifacts; public stories/BOLTs receive
sanitized summaries and links. Commands and screens below are proposed future
features, not claims of deployed behavior today.

| BOLT | Agent-executed journey to review | Expected result shown in evidence |
|---|---|---|
| B01 | Run the installed Product create/show/list commands; update the purpose, then archive and restore the demo Product | The same Product identity and saved values appear on readback; lifecycle changes are explicit and no repository/environment is provisioned |
| B02 | Open Products, create a Product using name/purpose/intended users, edit it and reload | The saved values survive reload; optional setup does not obstruct first save |
| B03 | Open a Product with two prepared delivery scopes and switch between them | Product identity/team stays shared; each scope shows its own connections and plan |
| B04 | Start onboarding in the browser, save a section, then use its supplied Resume in CLI command | The same session, scope, saved choices and progress resume without repeating completed work |
| B05 | Select a prepared repository/environment connection and verify it; inspect the prepared expired/revoked example | Actual selected assets appear with truthful verification/access state; existing information is reused |
| B06 | Use the supplied Idea/Existing assets/Running product examples; inspect guidance and edit a field directly | Guidance asks for relevant gaps/choices, direct forms stay usable, and no suggestion silently changes confirmed values |
| B07 | Resume the prepared authorized admin/provider handoff | The same onboarding session continues and displays the actual completed or blocked outcome; no credentials appear |
| B08 | Save a 75.00 USD plan; import the prepared different amount/date suggestion and accept only the date | The amount remains 75.00 USD with its manual provenance; only the date changes and retains source provenance |
| B09 | Complete the prepared onboarding journey and switch clients to inspect deferred peer availability | Saved Product/sources/plan agree across clients; Context and L5 report explicit unavailability without a peer request, receipt or result; denied examples remain truthful. Real peer receipt/result acceptance is deferred to #8266 and #8149/#8150. |
| B10 | Add the demo Product to two Portfolios and inspect its prepared Initiative/Contribution relationships | Both memberships appear without duplicating the Product or changing its first-save requirements/access |
| B11 | Run the installed Insights context command for the prepared overlapping-Portfolio/as-of fixture | Output shows qualified lineage, effective-time context and coverage; the one 10.00 USD cost fact remains 10.00 USD |


## Record one decision, using the existing verification stories

B01 combines V01 #8243 and V03 #8247 in one packet/decision; B09 combines V10
#8267 and V11 #8268. Each other BOLT uses its one V story. The packet includes
D01's rollout receipt for B01. The new evidence criterion is AC-04/SCN-04 on
V01–V12 and AC-05/SCN-05 on V13; existing behavior criteria remain mandatory.

The agent publishes the packet after execution and cleanup. The human checks that
it demonstrates the BOLT outcome, covers all required criteria and uses the right
deployed artifacts. A required failure, missing result, stale/mismatched artifact
or failed cleanup prevents acceptance; a convincing screenshot cannot override it.
The human records accept/reject against the exact packet and plan revision in ADP;
the existing stories link that decision. This is one shared decision where two V
stories contribute, not one additional approval for each report.

A rejection returns the finding to the named delivery owner. Correct the feature
or checks, deploy when needed, rerun affected checks and publish a new versioned
packet; retain the rejected evidence. Only dependent BOLTs wait for that decision.
Independent ready branches can continue within the eight-action limit.

There are **11 routine BOLT evidence reviews**, plus ADP's **one initial flow
approval**. Design, readiness and release-readiness have no human gate. Deployment
and technical prerequisites are checked automatically. Module completion rolls up
the accepted BOLTs and parent coverage without a final human sign-off.

## Optional rerun example

The agent's B01 packet may include concrete private values for the proposed
[CLI commands](../../../product-onboarding/design/experience.md#exact-proposed-cli-tree-and-flags):

```sh
adp product create --name "B01 acceptance" --purpose "Verify saved Product identity" \
  --intended-user "Acceptance reviewer" --starting-point idea --new-project \
  --idempotency-key "$CHECK_KEY" --json
adp product show --product "$PRODUCT_ID" --project "$PROJECT_ID" --json
adp product list --json
```

For B11 the proposed installed read command is:

```sh
adp insights context show --project "$PROJECT_ID" --product "$PRODUCT_ID" \
  --as-of "$AS_OF" --portfolio "$PORTFOLIO_A" --portfolio "$PORTFOLIO_B" --json
```

The agent executes these journeys before requesting review; supplying the commands
alone is not evidence of execution.
