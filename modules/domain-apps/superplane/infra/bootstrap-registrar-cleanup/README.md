# Temporary registrar cleanup authorization

Use only after stopped-source qualification and protected expiry verification for
`bootstrap-pre-namespace-revoked-v1`. The native cleanup composition owns the
journal, claim and EKS revocation. This root supplies just two deletion actions on
one immutable access-entry ARN to the existing app installer role, expiring within
two hours. It does not change role trust or grant create/associate permissions.

Under the installer maintenance lock, prepare a private local state directory,
review the saved plan for exactly this one inline policy, then apply that plan.
Assume the installer role with a restrictive inline session policy naming the same
immutable ARN. Compose that scoped client with `EksGrants` and
`recover_registrar`; never call broad administrative deletion directly.

After positive native revocation and claim release, destroy this policy using a
reviewed saved destroy plan. Retain the private Terraform state, both plans and
cleanup receipts. The time condition also limits exposure after interruption.
