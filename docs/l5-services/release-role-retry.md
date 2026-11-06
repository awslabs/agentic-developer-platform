# Adding a release role to a settled delivery

A Security or DevOps delivery may have a recorded attempt from before its
contract explicitly identified `job_mutation_role_arn`. Adding that binding
changes the contract digest. The engine must verify the old attempt using the
contract that actually governed it, then enforce the newly accepted contract on
the next attempt.

The retry resolver supports this narrow transition after the previous attempt
has concluded and its actions have settled. Native plan lineage must connect the
two accepted versions. The old binding must have no explicit job role, and the
new binding must add one without changing any other release field. This includes
the target, workflow, baseline, source definition, predecessor requirements,
required checks and cleanup requirement.

The old release ledger and its digest stay unchanged. The next invocation keeps
the same release source and definition pins, receives a new claim generation,
and records the newly accepted contract digest. Workflow job authorization then
checks the newly bound role against the selected account and current delivery
authority. A previous receipt never authorizes a new AWS operation.

Replacing or removing an existing role, changing the release target or weakening
qualification requirements is outside this transition. Active executions,
unsettled actions, missing evidence and unverified plan lineage also remain
ineligible for retry. Budget limits, expiry and attempts are preserved by the
normal amendment and retry APIs.

The remaining release requirements follow the [shared L5 architecture](architecture.md).
This compatibility does not supply AWS permissions or complete runtime qualification.
