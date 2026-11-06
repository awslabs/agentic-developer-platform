# Delivery flow: <confirmed outcome>

Record the Product, delivery home, included repositories, waves, per-wave evaluation issue URLs, and any declared readiness gates. Record the proposed spend ceiling beside the Project's available budget (or state **Project uncapped, metered**); the policy remains proposed until the owner accepts the engine gate.

When registration succeeds, add the `flow_id`, `plan_version`, `plan_hash`, `acceptance_gate_id` and plan-directory PR URL from `submission.json`. Show the owner the command `adp flow gate approve GATE --expect-plan-hash HASH` with values substituted, but never run it as the authoring agent. If registration could not run, mark it **not registered** and provide the required laptop command instead. Do not publish environment identifiers or private evidence in a public repository's documentation.
