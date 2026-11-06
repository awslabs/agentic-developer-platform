# Native knowledge asset contract

AWS documents type-specific asset metadata in [Managing assets][assets] and
instruction behavior in [Agent instructions][instructions]. These documents
provide more detail than the SDK's generic JSON metadata field. ADP mapping
version 2 uses that documented contract:

| ADP selection | Native type | Required selection | Activation |
| --- | --- | --- | --- |
| `skill` | `skill` | `native_metadata.name`, `description`, `agent_types` | `status=ACTIVE` or `INACTIVE`; `skill_type=USER` |
| `instruction` | `agents_md` | `native_metadata.agent_type` | Always active; native instructions do not support deactivation |
| `context` | Unqualified | No implicit attachment or memory conversion | Unavailable |

For global scope, select `GENERIC`. For agent scope, explicitly select supported
agent types: `CHAT`, `INCIDENT_TRIAGE`, `INCIDENT_RCA`, `INCIDENT_MITIGATION`,
`INCIDENT_UI`, `PREVENTION`, `RELEASE_READINESS_REVIEW` or `RELEASE_TESTING`.
Skills accept one or more specific types; instructions accept one. `GENERIC`
cannot be combined with specific types. ADP does not infer the target or enable
tools from source text.

AWS allows one `agents_md` asset per Agent Space and agent type. ADP refuses to
replace another source's instructions. Instructions are a single `AGENTS.md`
file, conservatively bounded to 25,000 UTF-8 bytes. Skills use `SKILL.md`, explicit
name and description, and ADP's existing 1 MiB source bound. Direct file creation
uses the supplied metadata; ADP does not rewrite the source or invent frontmatter.

`GetAsset` and `ListAssets` return the skill's actual `status`; setting an ADP
`adp_active` flag does not activate a skill. Receipt and content readback verify
the exact native type, targets, activation, immutable version, source hash and
bounded archive. Update metadata has PATCH semantics, so unrelated stored fields
are not assumed to disappear. Unexpected readback retains an unresolved outcome.
Retries reconcile the original owned asset and do not create a replacement after
an uncertain write. Older mapping receipts cannot prove version 2 qualification.

The documented [asset IAM actions][iam] apply to an Agent Space. The reviewed
asset and permission guides do not specify a per-user asset visibility contract.
ADP therefore supports only `product_team` knowledge publication here and rejects
`only_me` before native access. ADP provenance fields are not provider access
controls. A private source must not enter a shared native space on the strength
of an `adp_visibility` label.

These mappings repair the existing source resolver, asset helper and independent
qualification readback. Production source synchronization still requires its
explicit configuration selection, admitted source receipts, durable binding
operation, current work/consent authorization and worker dispatch. Result-to-Context
publication is a separate installed path. Offline mapping tests do not prove live
source synchronization, native retrieval, two-user visibility or later Context use.

[assets]: https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-managing-assets.html
[instructions]: https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-agent-instructions.html
[iam]: https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security-devops-agent-iam-permissions.html
