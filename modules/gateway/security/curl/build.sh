#!/bin/sh
set -eu

bundle=/opt/curl-security
source_dir=/tmp/curl-security-source
case "${1:-}" in
  prepare)
    python "$bundle/prepare.py" download "$source_dir"
    printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
    chmod +x /usr/sbin/policy-rc.d
    apt-get update
    apt-get install -y --no-install-recommends dpkg-dev build-essential
    apt-get build-dep -y "$source_dir/curl_8.14.1-2+deb13u5.dsc"
    python "$bundle/prepare.py" prepare "$source_dir"
    ;;
  compile)
    # Same Debian OpenSSL flavor shipped in the runtime, including HTTP3.
    # Full nonflaky tests run offline; no nocheck or feature-removal flags.
    # Upstream SSH fixtures require the actual local account name.
    USER=$(id -un)
    LOGNAME=$USER
    export USER LOGNAME
    cd "$source_dir/curl-patched"
    DEB_BUILD_PROFILES=pkg.curl.openssl-only DEB_BUILD_OPTIONS=parallel=2 \
        dpkg-buildpackage -b -us -uc -j2
    mkdir -p /out
    cp ../curl_8.14.1-2+deb13u5+adp1_*.deb /out/
    cp ../libcurl4t64_8.14.1-2+deb13u5+adp1_*.deb /out/
    ;;
  *)
    echo 'usage: build.sh prepare|compile' >&2
    exit 2
    ;;
esac
