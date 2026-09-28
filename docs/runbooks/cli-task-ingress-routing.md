# Hosted CLI Task ingress through CloudFront

The served CLI uses the deployment URL for Task submission. On a CloudFront
frontend, `/api/v1/tasks` must reach the existing REST API's explicit POST route
and canonical ingress Lambda. The general `/api/*` behavior sends traffic
directly to the gateway ALB, where Task reads and controls exist but public Task
submission does not. That path returns 404 before admission.

With `enable_task_api_route=true`, the gateway Terraform stack reads the already
published `/adp/<environment>/gateway/apigw-invoke-url` and adds an exact
`/api/v1/tasks` behavior before `/api/*`. It uses the REST API stage, strips the
`/api` prefix with the existing function, disables caching, and forwards viewer
authorization while replacing Host with the origin hostname. Task child routes,
including event streams, continue using the ALB. No new admission implementation
or worker permission is involved. The shared REST origin does not enable GitHub
OAuth routing unless its existing broker flag is enabled.

The route flag requires the REST API and published stage to exist already; it
is a second-pass setting. Review the Terraform plan before applying: the expected
change is the REST origin and exact Task collection behavior. Keep existing
CloudFront WAF, certificates, origins, and streaming behavior intact. Applying
and propagation do not prove that a Task was admitted or completed.

The triggering E42 evaluation was `adp-e2e-20260926-024553-d0ce9d`, workflow run
`36212795493`. Its upload succeeded, but POST `/v1/tasks` reached the gateway and
returned 404. The original artifact is retained for same-key reconciliation;
never upload a replacement snapshot to recover that request. Harness PR #6293
fixes tenant-scoped journal discovery and retains structured trigger error fields
before the EC2 worker is removed. A missing journal now reports unknown
acceptance. Reconcile the original artifact and request before any paid retry.

Validation uses Terraform mock-provider plans for default behavior, Task-only,
broker-only, shared REST origin with VPC streaming, and missing-origin refusal.
These exercise the real CloudFront module without AWS changes. The existing SPA
regression confirms API paths keep their prefix/security function and are not
rewritten to frontend HTML.
