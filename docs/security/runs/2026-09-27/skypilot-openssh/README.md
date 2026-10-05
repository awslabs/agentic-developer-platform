# SkyPilot candidate after curl, rsync and OpenSSH repairs

The final overlay retains the complete SSH client/server/SFTP package set and
supplies the missing passwd entry for the deployedUID1000. Actual installed SSH
client/server transport and the packaged SkyPilot API passed isolated fixtures.
The existing source patch is limited toCVE-2026-60002; two other OpenSSH High
advisories remain open.

Raw scan: **32 Critical /184 High**. Exact vendor/source/binary reviews establish
54 curl,24 rsync and3 OpenSSH source-package fixed occurrences on this same image,
leaving **0 Critical /135 High reviewed native occurrences**. This is not a count
of unique cluster CVEs or a claim of zero vulnerabilities. The raw reports remain
preserved; no broad suppression or future-version assumption was used.

Source/package versions and seven SSH binary hashes match the authenticated
Debian build. Curl and rsync reviews are rerun against this new digest, including
their actual unchanged binaries, instead of transferring dispositions by tag.
The rsync review helper accepts an optional curl-review path to support this
composition while preserving the earlier evidence files.

The candidate is an explicit emergency overlay. Publication is recorded
separately; no SkyPilot live rollout has been verified. Ordinary component build
workflows do not automatically select it. Live Postgres, IRSA and provisioning
acceptance remain separate. Rollback restores the previous image and exposure.
