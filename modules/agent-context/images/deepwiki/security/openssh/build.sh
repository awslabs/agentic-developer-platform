#!/bin/sh
set -eu
cd /src/openssh
# Preserve Debian's complete hardening/configuration and run its unit/compat
# tests. Omit only the optional GNOME UI and installer packages, not clients.
export DEB_BUILD_PROFILES='pkg.openssh.nognome noudeb'
export DEB_BUILD_OPTIONS='parallel=2'
# dpkg-source unapplies its quilt series after the build. Retain the exact
# patched client sources used by compilation before that normal cleanup.
mkdir -p /out/patched-source
cp ssh.c sshconnect.c sshconnect.h sshconnect2.c /out/patched-source/
sha256sum ssh.c sshconnect.c sshconnect.h sshconnect2.c > /out/patched-source-sha256.txt
dpkg-buildpackage -b -us -uc
mkdir -p /out
cp /src/openssh-client_10.0p1-7+deb13u4+adp.security.1_amd64.deb /out/
cp /build-tools/source-lock.json /build-tools/NOTICE /out/
sha256sum /out/*.deb > /out/package-sha256.txt
dpkg-deb -f /out/*.deb Package Version Depends > /out/package-control.txt
