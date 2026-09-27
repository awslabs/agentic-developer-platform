#!/bin/sh
set -eu
export PKG_CONFIG_PATH=/opt/adp-quic/lib/pkgconfig
export CFLAGS="$(dpkg-buildflags --get CFLAGS)" CPPFLAGS="$(dpkg-buildflags --get CPPFLAGS)" LDFLAGS="$(dpkg-buildflags --get LDFLAGS)"
mkdir -p /out /build-openssl /build-gnutls
common='--prefix=/usr --libdir=/usr/lib/x86_64-linux-gnu --disable-static --disable-dependency-tracking --disable-symbol-hiding --enable-versioned-symbols --enable-threaded-resolver --enable-ntlm --enable-smb --with-gssapi=/usr --with-nghttp2 --with-nghttp3=/opt/adp-quic --with-libssh2 --with-ca-path=/etc/ssl/certs --with-ca-bundle=/etc/ssl/certs/ca-certificates.crt'
cd /build-openssl
/src/configure $common --with-openssl --with-ngtcp2=/opt/adp-quic > /out/configure-openssl.log
make -j2 > /out/build-openssl.log 2>&1
make DESTDIR=/stage-openssl install > /out/install-openssl.log 2>&1
cd /build-gnutls
/src/configure $common --with-gnutls --with-ngtcp2=/opt/adp-quic > /out/configure-gnutls.log
# Debian's GnuTLS flavor exports CURL_GNUTLS_3 while keeping SONAME .so.4.
# Preserve that ABI independently of the OpenSSL CURL_OPENSSL_4 flavor.
sed -i 's/CURL_GNUTLS_4/CURL_GNUTLS_3/' lib/libcurl.vers
make -j2 > /out/build-gnutls.log 2>&1
make DESTDIR=/stage-gnutls install > /out/install-gnutls.log 2>&1
python /build-tools/package.py
