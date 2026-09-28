# Schema-016 API maintenance artifact

This Dockerfile records the package-only repair deployed to the existing dev API
at schema 016. It retains the complete `/app` tree and changes only the verified
curl packages and their license/provenance records. Application-tree SHA256:
`617f890b345fbe5d8a73b2d380f4e03e64fbdcdf7247f2f0fb10d82a9bb018e6`.

Published image:
`879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-api@sha256:70edd98cb9ed0ec48ec34dade114d6315815194009296f8c54a74fc5be2a748e`.

The normal release requires schema 042 and remains the installer default. Do not
replace that lock with this maintenance artifact. An attempted normal-image
promotion was rolled back after startup rejected the old schema. The existing
observation secret gained a JWT signing key and the deployment references it;
no secret values are included here. TLS success, bad CA refusal, wrong-host
refusal, health/readiness and missing/invalid credential rejection were verified.
