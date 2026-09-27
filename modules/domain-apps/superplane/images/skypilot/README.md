# SkyPilot API security overlay

The Dockerfile starts from the published S03 SkyPilot 0.12.3 image, preserving
its setuptools repair and provider support. It applies Debian updates, pinned
compatible Python dependency fixes, official CPython 3.10.21 on the same Trixie
ABI, and checksum-pinned kubectl 1.35.9 (compatible with EKS 1.35).
The official Python stage excludes site-packages; existing cp310 provider
wheels remain installed and `pip check` must pass. The complete old stdlib is
removed before copying the replacement, avoiding obsolete modules/bytecode.

Build from this directory using an existing ECR-authenticated Docker config:

```sh
docker build -t skypilot-security:candidate .
python verify_container.py skypilot-security:candidate
```

The test needs PyYAML and Docker on the host. It starts the actual manifest
command with UID 1000, a temporary HOME and the manifest's USER, with network
access disabled and no cloud credentials. It checks health, restart, task
parsing, client/API status round trips, Python native-library imports,
`pip check`, and kubectl. SQLite is used only for this isolated test; deployment
still requires its existing Postgres secret, cloud identity and acceptance.

`USER=sky` in the manifest is required because UID 1000 is absent from the
upstream passwd database: HOME resolves paths, but does not satisfy
`getpass.getuser()` during SkyPilot import.

Cryptography/pyOpenSSL and Pillow remain pinned by upstream provider SDKs
(VastAI, RunPod, OCI and MSAL). Installing their independently fixed versions
causes `pip check` failures; do not override those constraints or remove a
supported provider to obtain a smaller scan count. These findings remain open.
Curl, rsync, OpenSSH and other remaining findings also require further work.

The S03 `build_oci.py` remains the historical reproducible base builder. This
Dockerfile is the maintained overlay. See the dated candidate/publication
receipts under `docs/security/runs/2026-09-27` for exact identities and remaining
matches. A clean build/test is not evidence of live rollout.
