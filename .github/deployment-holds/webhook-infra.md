# Webhook infrastructure rollout hold

Issue #5195 tracks the protected-worker migration; #5176 prepares its scoped
prerequisites. Merging #5197 only publishes the IAM definitions. The webhook
deployment workflow must not apply the whole Terraform module while these
release conditions remain open.

While this file exists, infrastructure changes and manual webhook dispatches
are held before packaging, state migration/import, or apply. Mixed code and
infrastructure changes are held together. Code-only pushes can still deploy
Lambda code. The hold applies to subsequent commits as well as this merge.

Remove this file in a reviewed rollout change only after reconciling #5176,
validating compatible immutable images and shared marker/Door mediation,
reviewing a fresh scoped plan, and recording the controlled canary and IAM/EKS
migration evidence in #5195. Keep activation flags off until those conditions
are satisfied. Removing this file alone does not dispatch an apply.

This guards `webhook-ingress-deploy.yml`; it does not replace the deployment
guide or authorize manual Terraform commands or other deployment entry points.
