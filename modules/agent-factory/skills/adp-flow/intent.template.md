# Confirmed intent: <short outcome>

Describe the measurable outcome, selected Product, delivery home, expected boundaries and decisions confirmed by the owner. The JSON record below is consumed by `scripts/plan_writer.py`; replace the fictional example with confirmed issue aliases and repositories, never fabricated installation IDs.

```json
{
  "epic": "epic",
  "title": "Example delivery",
  "description": "Deliver a feature with independently checked waves.",
  "story_repositories": {
    "story-one": "example/home",
    "story-two": "example/component"
  },
  "waves": [
    {"ref": "w1", "title": "Foundation", "description": "Add the foundation", "stories": ["story-one"], "evaluation": "eval-one"},
    {"ref": "w2", "title": "Integration", "description": "Integrate the component", "stories": ["story-two"], "evaluation": "eval-two"}
  ],
  "edges": [],
  "policy": {"limits": {"max_spend_usd": "600"}}
}
```

For a wave with deployment or operations authority, declare `actions` drawn from the policy's allowed actions. For external spend beyond agent runs or irreversible work, declare `effects`. Every such wave needs a confirmed human checkpoint, for example:

```json
{
  "ref": "w3",
  "title": "Deployment",
  "description": "Deploy only after the owner confirms readiness",
  "stories": ["story-deploy"],
  "evaluation": "eval-deploy",
  "actions": ["deploy"],
  "effects": ["external_spend"],
  "readiness_gate": {"title": "Owner confirms deployment readiness"}
}
```

Incorporate that wave into the first JSON record only if confirmed, with its repository mapping and published issue aliases. Omitted wave actions remain code-only; omitted effects mean none. The gate follows the previous evaluation and precedes all stories in that wave, independently of the engine's initial acceptance gate. Operational policy authority without a corresponding declared gated wave is refused. A confirmed `policy.expires_at` must be a timestamp with a timezone later than `facts.emitted_at`; it is preserved exactly. Omit it only when the owner confirms the fourteen-day default.
