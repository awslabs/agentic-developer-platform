#!/bin/sh
set -eu
# Execute in the build target with --network none. These suites start only
# local protocol fixtures. Root-only SSH skips are reported by upstream.
cd /build-openssl
make test-nonflaky TFLAGS='-n -j2'
cd /build-gnutls
make test-nonflaky TFLAGS='-n -j2'
# Upstream automake assumes shared-build .libs/*.o. Our production objects are
# static PIC objects in lib/*.o. Override only test linkage/dependency paths.
cd /src-nghttp3
make -C tests check main_DEPENDENCIES= main_LDADD='../lib/*.o ../lib/sfparse/*.o'
cd /src-ngtcp2
make -C tests check main_DEPENDENCIES= main_LDADD='../lib/*.o'
