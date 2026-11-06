# Headless release progress

Full release acceptance and independently executable checks have separate
outputs. A successful headless check records only the work it observed; it
cannot complete a delivery contract or establish native UI acceptance.

The shared release checks the selected service's protected fixture, source pin,
infrastructure inputs and required credentials before publishing or installing.
An absent service fixture cannot fall back to another service's secret. Runtime
preflight still validates complete schemas, current sessions and authority.

Browser checks require the protected environment variable
`ADP_L5_BROWSER_AUTOMATION=allowed`. It defaults to disabled and cannot be set by
a fixture or workflow input. AWS management-console automation remains a
separate protected policy. DevOps uses ADP login and SSO into its native UI;
the existing first-party handoff check does not verify arrival in that UI.
Do not enable management-console automation to satisfy DevOps SSO acceptance.

[Security headless progress](security-headless-progress.md) compares disabled
configurations through API, CLI and MCP, then verifies foreign-tenant denial.
It starts no provider jobs.

DevOps can independently read existing Product runs and their original native
provider tasks:

```sh
python modules/L5-services/scripts/qualify-devops-headless.py \
  preflight REVIEWED_SHA PRIVATE_FIXTURE PRIVATE_EVIDENCE_DIRECTORY
python modules/L5-services/scripts/qualify-devops-headless.py \
  run REVIEWED_SHA PRIVATE_FIXTURE PRIVATE_EVIDENCE_DIRECTORY
```

This uses the existing DevOps manifest with a current `headless` scenario
authorization, selected `installed_journeys.runs` for `product`, `cli` and `mcp`,
and an independent read credential. Browser state, UI input, provider hostname
and ingress-policy inputs are not needed. The run reads existing records only;
it never submits new work. The private output retains the selected manifest,
readbacks and `progress.json` with `qualified: false`. Reusing an output attempt
fails before any request. Keep files mode 0600 and directories mode 0700 outside
the checkout.

The selected run labels do not prove new client execution. Installed-release
identity, fresh API/CLI/MCP execution, UI/SSO arrival, recovery, Context reuse,
portability, full acceptance and cleanup remain pending. Native actions need
separate scoped admission and spending controls; a fixture's numeric limit by
itself is not evidence that provider spending is enforced.

Well-Architected and Transform currently produce limited readback reports.
The shared publisher retains actionable incomplete diagnostics for these
formats, including their missing acceptance work, without turning them into
successful delivery evidence. FinOps also retains its explicit incomplete
native acceptance. A missing collector or downstream integration is an
implementation task, not a reason to invent fixture endpoints or retry an
unchanged release.
