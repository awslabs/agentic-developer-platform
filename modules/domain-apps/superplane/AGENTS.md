# Superplane infrastructure ownership

Keep all Superplane-specific infrastructure definitions and provisioning logic
inside `modules/domain-apps/superplane/`. Read `DESIGN.md` and `infra/README.md`
before changing infrastructure or deployment behavior.

- Define persistent AWS resources, IAM roles/policies and EKS access mappings in
  app-owned Terraform under `infra/`. Keep Kubernetes definitions under this app
  and their installation/recovery logic in its maintained entrypoints.
- Keep app environment inputs, build declarations and infrastructure utilities
  here. Repository-level GitHub workflows may call these entrypoints; shared
  Terraform roots may compose app-owned modules with explicit inputs.
- Reference the existing ADP cluster, database, state bucket, identity providers
  and shared automation roles as dependencies. Do not make app state own their
  deletion or duplicate resources already owned by platform state.
- A source relocation does not transfer live state ownership. Preserve names,
  backend keys, role trust and access scope; supply Terraform address migrations
  when module addresses change. Inspect plans for unintended deletion,
  replacement or permission expansion before applying them.
- Use the authorized installation operator to apply infrastructure. App runtime
  roles receive their own scoped authority. Do not require a new ADP credential
  connection merely to use an explicitly authorized local AWS operator.
- Temporary workspace/bootstrap grants remain owned by the app's recorded
  lifecycle operation and cleanup path; do not replace them with standing admin
  grants. Keep real plan/approval/identity checks intact.

Shared platform hooks may select or order the app, but must not contain a second
implementation of its provisioning. Document external integration contracts and
any remaining ownership gaps; source delivery is not proof of a live deployment.
