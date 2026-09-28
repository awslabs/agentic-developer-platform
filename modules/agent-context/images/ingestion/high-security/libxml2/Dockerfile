FROM public.ecr.aws/docker/library/python:3.13-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential pkg-config liblzma-dev zlib1g-dev patch && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:a2c9ae7b770da34860050c309f903221c67830c86e4a7e760692b803df95143a https://download.gnome.org/sources/libxml2/2.13/libxml2-2.13.9.tar.xz /tmp/libxml2.tar.xz
WORKDIR /src
RUN tar -xJf /tmp/libxml2.tar.xz --strip-components=1
COPY security.patch /tmp/security.patch
RUN patch --fuzz=0 -p1 < /tmp/security.patch
RUN ./configure --prefix=/usr --libdir=/usr/lib/x86_64-linux-gnu --without-python --disable-static --with-legacy --with-http --with-ftp && make -j2
FROM build AS tested
COPY callback-overflow-test.c /tmp/callback-overflow-test.c
RUN cc -I/src/include /tmp/callback-overflow-test.c -L/src/.libs -Wl,-rpath,/src/.libs -Wl,--export-dynamic -lxml2 -o /tmp/callback-overflow-test && /tmp/callback-overflow-test
RUN make check > /tmp/libxml2-check.log 2>&1 || { tail -100 /tmp/libxml2-check.log; exit 1; }
RUN make DESTDIR=/stage install && mkdir -p /package/usr/lib/x86_64-linux-gnu /package/usr/share/doc/libxml2 /package/DEBIAN /out && \
    cp -a /stage/usr/lib/x86_64-linux-gnu/libxml2.so.2* /package/usr/lib/x86_64-linux-gnu/ && \
    cp Copyright /package/usr/share/doc/libxml2/copyright && \
    printf 'Package: libxml2\nVersion: 2.13.9-0+adp3\nArchitecture: amd64\nMaintainer: ADP Security <security@localhost>\nDepends: libc6 (>= 2.38), liblzma5, zlib1g\nSection: libs\nPriority: optional\nDescription: libxml2 ABI 2 with upstream September 2026 security backports\n' > /package/DEBIAN/control && \
    dpkg-deb --build --root-owner-group /package /out/libxml2_2.13.9-0+adp3_amd64.deb && cp /tmp/libxml2-check.log /out/
FROM scratch
COPY --from=tested /out/ /out/
USER 65532:65532
