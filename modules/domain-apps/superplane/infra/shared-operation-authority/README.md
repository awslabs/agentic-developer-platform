# Shared operation authority

This app-owned child module defines Superplane producer/worker registration,
Gateway queue and database-read permissions, and namespaced runtime metadata
RBAC. The protected webhook Terraform owner composes it with explicit inputs.
The registry, Gateway role, shared cluster and shared state keep their owners.

The shared root contains six whole-resource `moved` blocks, including indexed
instances. Existing names, registration documents, backend keys, trust and
permission scopes remain unchanged. Review the saved plan before applying;
source relocation does not establish that any live state has migrated. No
registration replay or resource replacement is expected from relocation.

The registration command lives in `scripts/`; its former webhook path is a
compatibility wrapper. Run `terraform init -backend=false` and `terraform test`
for the mocked authority checks. The shared root separately tests Gateway
configuration composition, and the existing registration tests exercise this
canonical command against a mocked AWS service.
