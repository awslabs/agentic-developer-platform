FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS openssh-builder
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::AllowInsecureRepositories=false update \
    && apt-get -y --no-install-recommends install dpkg-dev git gpgv \
    && apt-get -y --no-install-recommends -Ppkg.openssh.nognome,noudeb build-dep openssh=1:10.0p1-7+deb13u4
WORKDIR /src
RUN apt-get --download-only source openssh=1:10.0p1-7+deb13u4 \
    && mkdir -p /out/source-authentication \
    && cp /var/lib/apt/lists/*InRelease /out/source-authentication/ \
    && sha256sum /var/lib/apt/lists/*Sources* /usr/share/keyrings/debian-archive-keyring.gpg > /out/source-authentication/metadata-sha256.txt \
    && apt-cache showsrc openssh > /out/source-authentication/apt-showsrc.txt
COPY platform/security/openssh-high/build-tools/ /build-tools/
RUN python /build-tools/authenticate-source.py && python /build-tools/prepare.py
RUN --network=none sh /build-tools/build.sh

FROM scratch
COPY --from=openssh-builder /out/ /out/
USER 65532:65532
