FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkgconf libpopt-dev libacl1-dev libxxhash-dev libzstd-dev \
    liblz4-dev libssl-dev libidn2-dev zlib1g-dev gnupg rsync fakeroot acl attr
ADD --checksum=sha256:c55f9c9dc10fb8bec397b399a0fdded53cc9a2d8e30891bb0d63724d25c37bef https://github.com/RsyncProject/rsync/releases/download/v3.5.1/rsync-3.5.1.tar.gz /tmp/rsync.tar.gz
COPY release-key.asc rsync-3.5.1.tar.gz.asc source-lock.json /provenance/
RUN install -d -m0700 /tmp/gnupg && gpg --homedir /tmp/gnupg --batch --import /provenance/release-key.asc && \
    gpg --homedir /tmp/gnupg --batch --status-fd 1 --verify /provenance/rsync-3.5.1.tar.gz.asc /tmp/rsync.tar.gz > /provenance/signature-status.txt && \
    grep -F '[GNUPG:] VALIDSIG 8EDF88F4731A1E8B45D67FBE6909546B2C52EC2D ' /provenance/signature-status.txt && \
    mkdir /src /out && tar -xzf /tmp/rsync.tar.gz --strip-components=1 -C /src && \
    cp /usr/bin/rsync /out/rsync-debian-before
WORKDIR /src
RUN CFLAGS="$(dpkg-buildflags --get CFLAGS)" CPPFLAGS="$(dpkg-buildflags --get CPPFLAGS)" LDFLAGS="$(dpkg-buildflags --get LDFLAGS)" \
    ./configure --prefix=/usr --sysconfdir=/etc --with-included-zlib=no && \
    make -j2 && make DESTDIR=/stage install
# Run upstream security regression and ordinary transfer tests as an unprivileged
# user, retaining real failure/skip output. Runtime test containers use no network.
RUN useradd --uid 1000 --create-home tester && chown -R tester:tester /src
COPY package.py /package.py
RUN cd /tmp && apt-get download rsync=3.4.1+ds1-5+deb13u4 && \
    dpkg-deb -R /tmp/rsync_*.deb /package && python /package.py
FROM 879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@sha256:265b773bc187017b27c5b17373246bbbc0ff80982928cc1e73c5bd0acff7bf2c AS runtime
USER root
COPY --from=build /out/rsync_3.5.1-0+adp1_amd64.deb /tmp/adp-rsync.deb
RUN dpkg -i /tmp/adp-rsync.deb && rm /tmp/adp-rsync.deb
COPY --from=build /provenance/ /opt/adp-rsync-security/
COPY --from=build /src/COPYING /src/NEWS.md /opt/adp-rsync-security/
USER 1000:1000
