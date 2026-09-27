# Superplane executor AWS CLI bundle update — #6124

The executor installs signed AWS CLI 2.37.4 in place of 2.31.22 and verifies
both the existing pinned AWS signing identity and the exact ZIP SHA-256:
`0c59444563f4df735eeb5481f6165f95dae546c33761760d8be9855d5cfe2d12`.
The candidate runs on the existing Python-base executor image and reports
AWS CLI 2.37.4 / bundled Python 3.14.6, replacing bundled Python 3.13.7.

Raw directory scans use the same frozen Grype database with no ignore rules
or updates. The baseline extracted distribution has 47 matches (14 High,
26 Medium, 6 Low, 1 Negligible); the candidate has 15 (3 High, 9 Medium,
3 Low). Both complete raw scans are retained losslessly in adjacent gzip files.
Directory and image scans have different OS context/package attribution: the
preceding exact executor image identified 42 bundled-Python matches, while the
standalone baseline directory identifies 46 Python matches plus one wheel
match. These denominators are not interchangeable. A separate exact image
scan is required before claiming the full runtime change's result.

This change is a bounded bundle update, not clearance of the remaining Python,
base OS, executor pip, or broader Superplane image-family findings. All remaining
raw matches stay owned by #6124. No publication, workload rollout, live credential
probe, cloud API request, or shared Terraform state operation is performed.

Both exact baseline and candidate distributions pass 12 offline service-model
input skeleton checks across EKS, EC2, IAM, STS, S3 and SSM, including required
field assertions, plus invalid-command refusal. Tests run as UID 65531 with
network disabled, readonly root, temporary configuration paths, metadata access
disabled, no host credential mounts, and no actual service calls. Initial fixture
attempts used S3 get-object, whose custom command does not support skeletons;
the final S3 model check uses head-object. Exact launcher hashes and the complete
reusable fixture are recorded beside this receipt.

The exact combined executor image config `sha256:f59d080717867922ce2dcec3369268bfd443e81d198cfff25d147f0480bcf07e`
is independently verified from Docker-save bytes. Its raw image scan retains 189
findings: 53 High, 77 Medium, 45 Negligible, 14 Low.
The preceding exact Terraform-rebuilt image retained 218 findings. The installed
AWS CLI launcher matches the tested signed bundle; all 12 installed service-model
checks pass. Offline nonroot runtime checks retain exact Terraform and kubectl
hashes, pip/import/data checks, kubectl dry-runs, and authority-missing worker
refusal/service idle. The complete image report is retained losslessly.

### Renewed signing metadata and final image (2026-09-26)

The initial retained signature receipt contains `EXPKEYSIG`: the Ubuntu keyserver
served expired metadata even though GPG exited zero. That receipt is historical,
not sufficient current signature acceptance. The official AWS installation guide
supplies renewed metadata for the same pinned fingerprint. The build now vendors
those exact public key bytes, pins their SHA-256, and rejects expired, revoked,
missing, wrong-identity, or invalid signatures explicitly. Actual old-key rejection,
renewed-key acceptance, and archive-tampering rejection pass; three focused status
verification tests also pass. The key and signature receipts remain separate from
the ZIP hash check; no expiration waiver or signature-identity rotation is used.

The changed image was rebuilt from `518ac08b6b6e3a5551d661e137d39a621e1cdb33`.
Both scanners used the same Docker archive and independently report config
`sha256:a512c87543ee44054dc65ec473a662e0268fde09592ae600871317891dc81de2`.
The full raw inventory remains 189: 53 High, 77 Medium, 14 Low, 45 Negligible.
Actual offline nonroot/read-only runtime and all 12 installed service-model checks
pass. Installed launcher and Python library hashes equal the signed official ZIP;
Terraform and kubectl retain their exact previously reviewed binary hashes.

Fresh PSF CNA ranges and the signed installed Python 3.14.6 identity substantiate
eight old bundled-Python High source fixes. The wheel High package was removed
from the signed distribution and is absent in the exact-image SBOM. Three bundled
Python High matches remain: CVE-2026-11940, CVE-2026-11972, CVE-2026-15308. The last
is rated Critical by its GitHub advisory; native scanner High is retained separately.
Their minimum fixed stable 3.14 release is 3.14.7. The latest official AWS CLI tag
and freshly signature-verified latest ZIP are still 2.37.4 / Python 3.14.6, so this
upstream release dependency remains open. No signed bundle internals were modified.

`followon-superplane-awscli-renewed-image.json` retains exact scanner/runtime hashes,
per-advisory outcomes and original selectors, including new observations that have
no historical selector. All live acceptance and remaining family scope stay open.
