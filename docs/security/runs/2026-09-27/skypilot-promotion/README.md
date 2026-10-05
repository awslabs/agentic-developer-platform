# Promote the reviewed SkyPilot candidate

The release lock selects the final published curl/rsync/OpenSSH candidate. ECR
root and amd64 manifest byte hashes, config identity, actual passwd/home facts and
current vendored-package SBOM observations are recorded here or in the image fact
files. The candidate has0 reviewed Critical/135 High occurrences; raw scanner
32C/184H is preserved with the exact vendor/source/binary dispositions under
`../skypilot-openssh/`. No live rollout is claimed.

The startup contract now verifies the real UID1000 passwd entry required by
native SSH while keeping the historical missing-passwd expanduser mechanism test
and explicit writable HOME/config requirements. All97 focused release-pin,
manifest, startup and vendored-package tests passed. Full domain CI and live
Postgres/provisioning acceptance remain separate.
