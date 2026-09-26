# Gateway candidate for live Task regression fixes

Build-only receipt for source `91d3f20bfb5cb01a1cca162c85ac97eef05fa9c7`.
It combines integration source `c76280a4e` with the malformed Task lookup fix
(#6298) and bounded SDK coding-turn fix (#6300). No deployment was performed
while verifying this candidate; source CI/merge and rollout verification remain
separate gates.

- CodeBuild: `adp-dev-gateway-build:ef38bb90-042e-45e4-925b-06cf159bd005`, `SUCCEEDED`.
- Repository: `879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-gateway`.
- Image digest: `sha256:807b5348beb44dbd66dcd13f7b2e1d7435fb2db70c484da25ec3f522593ba2df`.
- Immutable tag: `91d3f20bfb5cb01a1cca162c85ac97eef05fa9c7`.
- Source archive SHA-256: `89ff3af55e6c0c463b6e1fced877ed189047a7ee0303e80422801a76560721d4`.
- Source ZIP: `s3://adp-terraform-state-879318057152/codebuild/src/adp-dev-gateway-build/91d3f20bfb5cb01a1cca162c85ac97eef05fa9c7-1790392727-3543939.zip`.
- Log group: `/aws/codebuild/adp-dev-gateway-build`; stream: `ef38bb90-042e-45e4-925b-06cf159bd005`.

Independent fresh AWS reads confirmed the successful build, exact source
location, immutable repository/tag and image digest. The downloaded source ZIP
is byte-identical to a fresh `git archive --format=zip` of the source SHA. ECR
manifest and configuration bytes hash to their referenced digests. Image
configuration reports Linux/amd64 and `GATEWAY_RELEASE` equal to the source SHA;
this gateway image uses that environment marker, not OCI revision labels.

The coding-turn change aligns the turn store with the existing SDK persona
allowlist while preserving Task identity, deadline and turn-limit checks.
Malformed public Task lookup IDs return absent before storage access; actual
storage failures remain errors. Independent isolated tests passed all 30 Task
runtime cases and 12 read-adapter cases. The integration-only lifecycle test
repair passed 13 real disposable PostgreSQL tests without changing runtime
sources. These checks do not establish a successful live coding/model run.

The original Claude Task was admitted and observed through its event stream,
then failed at its first turn before successful model execution was confirmed.
That original outcome remains failed. This candidate does not replay it, admit a
replacement Task, update workers, publish the separate Superplane API, or change
IAM/infrastructure.
