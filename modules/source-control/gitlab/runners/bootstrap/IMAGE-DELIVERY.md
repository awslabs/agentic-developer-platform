# Job image delivery contract

The host has no registry credential, host mapping or runtime registry egress.
The registry name in a pinned image reference identifies a locally preloaded
Docker digest; it is not a runtime destination. The authorized S3 image builder pulls the
reviewed job image and builds the non-root helper from the pinned upstream base
using temporary, build-only private-registry authentication. It must never
snapshot a Docker login file, image-pull credential, instance profile or registry
credential onto the job AMI. Keep Docker's pulled image store on the root volume
that will be snapshotted; `docker save`/`docker load` is not sufficient because
it may lose the repository digest address required by the runner.

Before publishing the private AMI, restart Docker, then run
`python3 bootstrap/verify_images.py '<job_image>' '<helper_image>'` on the
builder and again on an instance launched from the resulting AMI. Both images
must be locally inspectable under the **exact** approved repository digests,
with Linux/amd64 and non-root user `10001:10001`. The approved job image
must prepare `/builds` and `/cache` as writable for that user. The helper
recipe prepares these and `/gitlab-runner-cache-init`, the exact path where
Runner 19.5.2 mounts a fresh Docker volume before calling
`gitlab-runner-helper cache-init /gitlab-runner-cache-init`. With fresh empty
Docker-managed volumes, run this cache-init as user 10001 under no network,
no added capabilities and no-new-privileges, then mount the same volume at
`/builds` and verify a user-10001 write. Also test real helper clone, artifact
and cache operations before releasing a job: image inspection cannot establish
volume ownership or workload behavior.

Record the verified helper and job digests in `versions.lock.json`. Set the
region's `host_ami` entry only after image qualification, with
`images_sha256` computed as SHA-256 of the UTF-8 job digest, newline, helper
digest, newline (in that order); `image_verifier_sha256` must equal the checked
script digest. Tag the private AMI with these hashes under
`adp:gitlab-runner:images-sha256` and
`adp:gitlab-runner:image-verifier-sha256` along with the existing bootstrap
hash. Bake the checked verifier to `/usr/local/libexec/adp-job-images`.
The release checker compares the lock and AMI tags; host user-data checks the
script bytes and the **local** Docker image digests after Docker starts, before
opening SSH to the manager. The runner's `pull_policy = "never"` prevents
runtime registry pulls. Missing pins or failed image inspection keep the job
host closed. No registry or AMI is provisioned by this repository check.

## Manager credential image

The manager's reviewed `bootstrap_image` must run Python 3 with pinned `boto3`,
`botocore`, and `cryptography` dependencies. The checked loader and manager
guard source are mounted read-only from per-group ConfigMaps, not copied into
the image with any credential values. S3 must supply a verified image digest,
the pinned dependency lock and its SHA-256 before the release gate can open.
No package installation or registry login takes place on a running manager.

Before the chart starts registration, its non-root init container fetches the
exact named group token and CA through the manager's IAM role. It verifies the
runner with private HTTPS and the pod's memory-backed system ID, then places
the token and CA on separate memory-backed mounts. The chart reads the token
from `/secrets/runner-token`; Runner receives the verified CA at
`/secrets/ca.pem`. A missing or rejected CA, secret version,
or runner verification prevents the init container from completing. After
registration, Runner verifies its connection again using that CA, while the
manager guard compares the registered runner ID, token and on-disk system ID
against the loader's verified receipt. Only then does it publish the non-secret
runner ID and system ID under the group's SSM `registration-receipt` parameter
and permit Runner to start. The parameter begins as `pending`; operators must
check the group-specific receipt and a fresh manager heartbeat before enabling
jobs. Neither Terraform state nor Kubernetes Secrets contain credential
values. S3 and #9291 must still test the reviewed image, chart registration,
helper checkout, artifacts and cache transfers before activating jobs.
