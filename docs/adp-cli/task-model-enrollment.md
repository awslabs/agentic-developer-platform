# Model choices for hosted Tasks

Hosted Tasks use the existing model preference API and CLI. Their four persona keys are separate from the webhook dispatch registry:

| Task persona | Contract | Model transport |
|---|---|---|
| `agent-task-investigator` | `task-messages-v1` | Anthropic Messages |
| `agent-task-cyber` | `task-cyber-sdk-messages-v1` | Anthropic Messages with tools |
| `agent-task-claude-developer` | `task-coding-sdk-messages-v1` | Claude Agent SDK through Task Messages |
| `agent-task-codex-developer` | `task-codex-responses-compat-v1` | Codex Responses compatibility through Task Messages |

Codex uses the actual selected Anthropic model through the compatibility transport. It does not claim an OpenAI model was invoked.

Select the deployment and tenant before inspecting or saving a model:

```bash
adp --deployment dev --tenant TENANT models catalog --persona agent-task-investigator --json
adp --deployment dev --tenant TENANT models mappings set --persona agent-task-investigator --model CANONICAL_MODEL_ID --dry-run --json
adp --deployment dev --tenant TENANT models mappings set --persona agent-task-investigator --model CANONICAL_MODEL_ID --yes --json
```

Use a model ID returned by the catalogue. The preference remains owned by the authenticated human, and the saved revision is the model-policy version required by standing Task enrollment. A saved choice does not certify runtime availability: admission requires current membership, enrollment, budget headroom, routing, pricing, and fresh exact-profile provider evidence. Legacy SDK probe evidence cannot satisfy the Task contract. Task profiles have no implicit model default.

The bounded provider probe proves that the selected destination accepts the exact Messages payload. Full Claude/Codex issue execution, controls, usage attribution and cleanup remain separate EC2 acceptance scenarios. No Task persona is added to the legacy webhook dispatch allowlist by model enrollment.
