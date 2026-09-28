# Project conventions — code authoring
Read before editing. Match surrounding patterns, names and structure.
Make the smallest change that solves the task; avoid speculative features,
unrelated refactors, renames, dependency upgrades and import cleanup.
Code must compile and pass existing tests. Test new code's main paths,
failures and edge cases that could lose data. Handle errors explicitly.
Never hardcode secrets or credentials, or leave debug code.

## Human-readable findings and handoff

When a plan is required, lead with **My understanding of the task** and **How I
plan to implement it**. Start with the requested behavior, then explain what
each logical step accomplishes, why, how the steps fit and how you will check
the result. No required "who needs this" section. Assume the reader has not
read the issue, discussion or code. The explanation must stand alone without
reproducing the technical design: put paths, schemas, locks, helper names and
branch/checkpoint details afterward. Explain unavoidable terms by their purpose;
give enough detail without repeating requirements or imposing a word limit.
Distinguish unverified access from a missing resource. Show commands for different
terminals in separate labelled code blocks. Before posting, check that the reader
can explain the change, steps and verification without the engineering notes.

In the outcome, lead with the user-visible result and PR state. Separate tests
you ran from CI results and deployed checks. Name any rollout or migration
needed before users benefit. If another run already delivered the work,
distinguish its contribution from your verification or follow-up.

Do not put a full debugging diary or generic learnings section in the human
summary. Keep required handoff details in the linked record.
Apply this format to your findings or handoff. It does not grant authority to
post comments, coordinate other agents, or perform supervisor duties.
