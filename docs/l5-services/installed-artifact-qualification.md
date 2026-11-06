# Installed artifact qualification

`modules/L5-services/scripts/qualification_installed_artifacts.py` reads the
selected modular service's actual installed artifacts. It is reusable by
Well-Architected and Transform release drivers. It uses AWS APIs, the protected
IAM runtime-proof route, and served static asset reads. It does not use browser
automation, a local kubeconfig, pod exec, or a native provider Start call.

The active AWS session must be the exact release role already selected in the
private `installed_release` fixture. That role needs its existing scoped worker,
manifest, EKS read, runtime-proof invoke, frontend read, and Lambda factory-probe
authority. A denied read blocks qualification; it does not expand permissions.

Well-Architected enables the collector with a private top-level
`qualification_installed_artifacts` object containing these fields:

| Field | Meaning |
|---|---|
| `cluster_name`, `namespace` | Exact selected gateway cluster and namespace |
| `proof_invoke_url` | Selected IAM-protected API Gateway stage URL |
| `release_bucket` | Private immutable L5 release bucket |
| `frontend_assets` | Reviewed frontend asset path to SHA-256 map, including `index.html` |
| `cli_directory` | Absolute path to the installed CLI files |
| `cli_files_sha256` | Pins for `adp`, `adp_deployments.py`, `adp-service.py`, `adp_common.py`, `bg-cognito-auth.sh`, and `command-manifest.json` |
| `product_profile_enabled`, `product_onboarding_enabled` | Exact expected gateway admission settings |

The fixture's `frontend_asset_sha256` pins the entire reviewed asset map using
SHA-256 of UTF-8 `json.dumps(assets, sort_keys=True, separators=(",", ":"))`.
The collector discovers the selected deployment's CloudFront distribution via
SSM and verifies each served asset against that map. CLI files must be owned by
the current user, not symlinks, and not writable by other users. Their command
manifest digest must also match the gateway's authenticated runtime observation.

The release driver saves `installed-before-native-cases.json`,
`installed-after-native-case-1.json`, and `installed-after-native-case-2.json` in
the existing private evidence directory. Each checkpoint binds the release
context, collector selection, preceding checkpoint, exact Product run/result,
and independently read artifact identities. Interrupted runs retain the same
baseline; the collector cannot create a retrospective baseline after a Product
intent has been journaled. Installing this collector after both native cases
have already run cannot prove their historical artifact state.

`SnapshotSeries.checkpoint(stage, completed_case=case_identity(case))` is the
shared interface for service drivers. Call the first checkpoint before native
submission, and each subsequent checkpoint after its native case completes.
`SnapshotSeries.verify(cases)` rereads the installed release before emission.
For another service, the fixture work queue and context service must both name
that exact modular root. The canonical `runtime_names.functions` and
`queue_scope` helpers preserve Security's legacy shared names while using the
dedicated names for the other four services.

This evidence can complete the `same-artifacts` and `runtime-revisions` live
checks. It observes current migration heads and package/schema hashes, but it
does not establish prior-schema compatibility or a historical rollback. Those
criteria require their own retained experiment. It also does not replace
installed CLI/MCP execution, a human UI journey, provider-call accounting,
native fault recovery, or the final acceptance ledger. Snapshots of current
state must never be represented as those experiments.
