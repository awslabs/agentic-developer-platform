# AWS CLI source build

The executor builds AWS CLI 2.37.4's maintained `portable-exe` distribution with
Python 3.14.7. The official 2.37.4 ZIP still embeds Python 3.14.6, affected by
CVE-2026-15308, CVE-2026-11940 and CVE-2026-11972. CVE-2026-15308 retains Grype High and an unreviewed GitHub advisory Critical
rating; the PSF CNA record reports CVSS 4.0 High (8.7). All ratings remain
visible with their actual sources.

The Dockerfile pins the Python image digest, exact upstream source commit and
archive checksum. Upstream dependency files include hashes, enabling pip's
hash-checking mode; dependencies and CLI source are not patched. The installed
prefix includes source provenance and upstream/third-party licenses. Only the
portable executable tree is copied into the executor runtime. This is an
ADP-built distribution, **not an AWS-signed executable**.

Validate the complete image as non-root with read-only root, temporary HOME,
BG_CONFIG_DIR, XDG directories, AWS_CONFIG_FILE, AWS_SHARED_CREDENTIALS_FILE and
KUBECONFIG, with no host credential mounts and networking disabled. Required
checks include actual service-model input skeletons, invalid-command refusal,
executor import/startup refusal behavior, preserved Terraform/kubectl hashes,
and same-image SBOM and selected CVE reconciliation. Deployment acceptance is
separate and remains subject to existing holds.

The verifier and public key below remain available for reproducing the signed
predecessor evidence; they do not authenticate the source-built executable.

## Signed predecessor verification

`aws-signing.asc` is the public key embedded in the official AWS CLI installation
guide, retrieved 2026-09-26:
https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html

Its fingerprint remains `FB5DB77FD5C118B80511ADA8A6310ACC4672475C`.
AWS renewed its expiration to Unix time `1814472778`; the Ubuntu keyserver still
served metadata expiring at `1783435745`. That older metadata produced
`EXPKEYSIG` for the September 25 AWS CLI 2.37.4 signature even though GPG exited
zero. The verifier now pins the reviewed official key bytes and ZIP, verifies in
an isolated keyring, and requires GOODSIG/VALIDSIG without expiration, revocation,
or error statuses. Expiration is not waived by the ZIP checksum.

Key SHA-256: `b3cef249c50f7e26254ffd91bc7453d7424247cc98c372840e70297060c0e146`.
The key is public; no signing secret or credential is included.

## Runtime libuuid maintenance

The tools build can bundle an older util-linux library than the selected
executor runtime base. After copying the portable executable, the runtime build
replaces only its `libuuid.so.1` with the exact reviewed `libuuid1` payload from
that base. `libuuid-lock.json` binds the original and replacement bytes, package
version, architecture and file paths. The installer checks dpkg ownership and
the vendor fixed-version minimum before copying; a different base payload or
upstream bundle fails the build and needs renewed review.

The replacement retains the complete ELF bytes, including its original build ID
and source-package note. `libuuid-maintenance.json` records the additional ADP
maintenance separately from the AWS CLI source-build record. No metadata is
removed and no scanner findings are suppressed. Source-package notes remain
embedded identities distinct from root dpkg ownership during qualification.

The ordinary compatibility probe loads the bundle's own Python 3.14.7 and
`_uuid` extension, calls `generate_time_safe`, and checks that the versioned
`UUID_2.20` symbol was loaded from the bundled library path. It is compiled in
the tools stage and bind-mounted only for the final nonroot, network-disabled
build check; the probe is not installed in the runtime. Independent qualification
also compares all original exported/versioned symbols, the SONAME, actual CLI
startup/service-model behavior and installed library hashes against the selected
runtime-base package. The final merged-source image still needs a fresh raw scan.
