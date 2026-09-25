# Preserve runtime settings during image delivery

`deploy-image.sh` requires a reviewed private runtime profile when the Gbrain
Terraform state or ECS service already exists. The profile is passed after the
checked-in dev defaults to both the build-prerequisite apply and final activation.
Rollback uses the same profile and verifies its hash before registry lookup or
Terraform operations. A first bootstrap with neither state nor service may use
the default image entrypoint and environment.

Keep the profile outside the repository, with mode `0600`, in the deployment
operator's persistent configuration directory. It is JSON Terraform variables,
with these required fields:

- `container_command`: the existing container command, or `null` if omitted.
- `container_entrypoint`: the existing container entrypoint, or `null` if omitted.
- `container_environment`: the complete ordered array of `{ "name", "value" }`
  entries from the existing task definition, including deployment-specific values.
- `service_subnet_ids`: all subnets from the live service network configuration.

Read these fields using `ecs describe-services` and `ecs describe-task-definition`
for the intended service and revision. Secret references remain managed by the
Terraform module; do not resolve or copy secret values into the profile. Review
the captured values and record the SHA-256 alongside the deployment receipt.
The script checks the JSON shape and hash without printing environment values.

Use the same path and reviewed hash for every update, retry and rollback:

```bash
export GBRAIN_RUNTIME_TFVARS=/absolute/private/path/gbrain-runtime.tfvars.json
export GBRAIN_RUNTIME_TFVARS_SHA256=<reviewed-sha256>
bash modules/research/gbrain/scripts/deploy-image.sh
# Roll back only the image, preserving the same runtime profile:
GBRAIN_IMAGE_DIGEST=sha256:<previous-digest> bash modules/research/gbrain/scripts/deploy-image.sh
```

Missing/unreadable profiles, missing runtime fields, hash mismatches and failures
to establish whether existing state/service exists stop before mutation. Review a
saved Terraform plan before live delivery; the intended image update should
preserve runtime, networking and other resources. Keep the profile and its hash in
the operator's deployment configuration so a later invocation cannot silently
fall back to module defaults.
