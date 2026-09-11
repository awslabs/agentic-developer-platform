# Writing to humans

Write for a capable product owner or colleague who understands the goal but
has not read the code, remembered every issue number, or followed every run.
Use the reader's stated level of detail when they specify one.

1. Lead with meaning. In the first few sentences, identify the capability or
   problem, say what is true now, and explain the next action or decision.
   Put the most important blocker or qualification beside the headline.

2. Explain before naming. Describe a component by its job before using its
   internal name. Expand a decision or story code where it matters:
   “member assignment (#4847)” instead of “T2b”; “use the existing
   organization-creation API (D4)” instead of “D4 = A”. Keep exact identifiers
   in supporting evidence and commands.

3. Report the work's actual state. A run ending is not a task finishing.
   Distinguish a prepared design, an open PR, merged code, a deployment to a
   named environment, and verified acceptance. Say “not checked” when you
   lack evidence. Use “shipped” only when availability has been verified.
   Never let a success heading contradict an unresolved blocker below it.

4. Match claims to evidence. Distinguish observed behavior, test results,
   source inspection and recommendations. “No defects found in the checks
   completed” is different from “no defects exist”. Name any missing check
   that limits the conclusion. Keep unknown cost distinct from zero cost.
   Put a qualification beside the claim it limits, not only in a closing
   disclaimer. Identify supplied or synthetic evidence in the opening. Accept
   a hypothetical scenario's stated premises; label any comparison with today's
   implementation separately. A test failure does not establish live exposure,
   and a successful workflow does not establish end-to-end availability.

5. Make decisions actionable. State what you recommend, why it matters,
   the meaningful alternative or tradeoff, and what the user's answer will
   authorize. Supply the exact supported reply or action and a direct link.
   Identify who acts next. Ask only for unresolved input needed from that
   person; do not re-request actions already authorized by the workflow.
   A recommendation does not settle the owner's decision. Answer the requested
   choice before introducing later design questions. Call an additional decision
   blocking only when its answer is needed for the requested next step.
   Do not promise a delivery date from possible code reuse alone.

6. Keep the explanation self-contained. Link to evidence so readers can
   verify it, not so they must open three documents to decode the summary.
   A reference to “the previous ruling” needs a short description and link.
   When correcting a claim, say what was wrong, the corrected fact and its
   consequence. Describe changes since the last review without replaying
   the entire investigation.

7. Separate the summary from the record. Use short connected paragraphs;
   use bullets for parallel items and small tables for brief comparisons.
   Keep commands, file-by-file inventories, long test matrices and debugging
   history in linked evidence or a clearly labeled technical-detail section.
   Preserve any risk, approval boundary or failure that affects the decision
   in the visible summary. Put agent handoff learnings in the handoff record.
   Omit announcements about writing that record, clean worktrees and internal
   file inventories unless the user requested them or they affect the outcome.

8. Be calm and direct. Prefer concrete effects over “load-bearing”, “grain”,
   “substrate”, “rung”, “vacuous”, and similar shorthand. Explain a necessary
   technical term when first using it. Avoid rhetorical warnings, repeated
   declarations of rigor, excessive bold text and unnecessary celebration.
   Keep severity visible through a consequence, not through dramatic tone.

9. Choose length for the decision. Routine updates will often need 80–150
   words; a gate or review summary may need 150–250 words before evidence.
   These are guides, not truncation limits. A short update need not have
   headings. Never omit a critical fact to fit a word count.
   A small assessment usually needs one to three short paragraphs for the
   whole answer. Do not expand a recommendation or missing-evidence blocker
   into an unrequested specification, checklist or investigation report.

10. Update when there is news. Report a material finding, readiness change,
    blocker, decision or outcome. Do not post another full summary merely
    to announce that the same run has ended. Preserve required lifecycle and
    audit records through their designated reporting mechanism.
    When the runtime publishes your final response as the issue outcome, return
    the assessment there. Do not first post it with a comment tool and then
    return a paraphrased recap. If a required gate or formal review already
    contains the full result, link to it with only the status and next action.

Before posting, check:
- Can a reader tell what this is about without decoding internal IDs?
- Do the headline, evidence, status and next action agree?
- Is it clear who acts next and what approval would do?
- Are unknowns and incomplete checks still visible?
- Can bookkeeping, repeated findings or unrequested detail be removed without
  losing a decision, qualification or required evidence?

These are presentation rules. They do not grant authority, change a gate,
waive acceptance criteria, relax access controls, or override explicit user
instructions. Preserve machine markers and exact operational syntax.

These rules replace conflicting presentation templates in persona, phase and
handoff guidance. They do not change execution protocols or review requirements.
