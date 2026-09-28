# OpenSSH server High follow-up

The authenticated Debian 10.0p1 source retains the existing CVE-2026-60002 client-lifetime fix and adds upstream server fixes for CVE-2026-59999 (DisableForwarding takes precedence over PermitTunnel) and CVE-2026-60000 (GSSAPI failure accounting and handler cleanup). Client, server and SFTP packages advance together to 1:10.0p1-7+deb13u4+adp.security.2. Debian GSSAPI support, hardening, configuration and capabilities remain enabled.

Build the package artifact from repository root with `docker build -f platform/security/openssh-high/Dockerfile .`. APT authenticates the Debian source; source-lock.json additionally pins archive and patch SHA256 values. The backports omit upstream RCS banner hunks and retain Debian's u_int32_t context spelling. Both apply with zero fuzz. Debian's complete package build includes upstream unit and compatibility tests.

The runtime overlay installs all three coordinated packages and removes only newly generated build-time host keys, preserving existing runtime configuration. The normal nonroot SkyPilot UID remains 1000. The separate loopback fixture uses synthetic keys, no exposed ports or operator credentials, and tests installed client/server/SFTP plus Git clone/fetch/push, rekey and rejection cases.

Raw scans retain source-version matches. Exact package contents, installed binary hashes and source provenance must be checked before applying any disposition; this source directory alone is not live closure.
